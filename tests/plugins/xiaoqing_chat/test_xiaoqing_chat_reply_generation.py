"""回复生成上下文、超限重建和草稿结构。"""

from __future__ import annotations

import pytest
from PIL import Image

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.ai import AIError
from plugins.xiaoqing_chat.chat_event import event_chat_id
from plugins.xiaoqing_chat.media.models import RenderedMedia
from plugins.xiaoqing_chat.reply_payload import build_reply_payload_from_parts
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    chat_message_event,
    draft_reply,
    scripted_chat_context,
    seed_chat_history,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


# 每条回复都送审，审查模型的材料里能看到这一轮用的目标。
_ALWAYS_CHECK = {"reply_check": {"llm_checker_mode": "always", "max_regen": 0}}


@pytest.mark.asyncio
async def test_reply_route_gets_current_image_and_retains_the_text_projection(
    mock_context, sample_group_event, tmp_path
):
    path = tmp_path / "photo.png"
    Image.new("RGB", (2, 2), "blue").save(path)
    summary = "[图片：一张蓝色的图]"

    async def reply(call):
        adapter = call.kwargs["adapt_messages"]
        direct  = await adapter(call.messages, ("text", "image"))
        assert direct[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        fallback = await adapter(call.messages, ("text",))
        assert fallback == call.messages
        assert summary in fallback[1]["content"]
        return "看到了。"

    model = ScriptedChatModel(reply=reply)
    scripted_chat_context(mock_context, model, {"reply_check": {"enable_reply_checker": False}})
    event                             = chat_message_event(sample_group_event, "看这张图")
    event["_xc_effective_user_parts"] = (
        {"kind": "text", "text": "看这张图"},
        {"kind": "image", "description": "一张蓝色的图", "file_path": str(path)},
    )
    event["_xc_rendered_media_items"] = [
        RenderedMedia("image-hash", "image", "一张蓝色的图", (), summary, cached_path=path)
    ]
    draft = await draft_reply(mock_context, event, text=f"看这张图{summary}")

    assert draft is not None and draft.text == "看到了。"
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generator_sees", "checker_sees", "described"),
    [
        # 多模态生成 + 多模态审查；入站摘要只有笼统描述。
        ("pixels", "pixels", True),
        # 多模态生成 + 审查降级到纯文本：旧摘要漏了细节，审查要一份新转写。
        ("pixels", "transcription", True),
        # 生成和审查都降级到纯文本：没有可用摘要，两边用同一份转写。
        ("transcription", "transcription", False),
    ],
)
async def test_review_checks_image_details_with_the_images_the_generator_saw(
    mock_context, sample_group_event, tmp_path, generator_sees, checker_sees, described
):
    """生成模型从图里读到入站摘要没有的细节和链接；审查与预检拿到同一份图或转写，回复照常发出。"""

    from tests.helpers.reply_checker_test_support import checker_verdict

    path = tmp_path / "notice.png"
    Image.new("RGB", (2, 2), "white").save(path)
    summary                = "[图片：一张漏洞公告]"
    detail                 = "Fixed in: 1.27.4"
    link                   = "https://nginx.example/advisory-31337"
    checker_messages: list = []

    async def reply(call):
        modalities = ("text", "image") if generator_sees == "pixels" else ("text",)
        await call.kwargs["adapt_messages"](call.messages, modalities)
        return f"截图上写着 1.27.4 修好了，公告在 {link}"

    async def checker(call):
        adapter = call.kwargs.get("adapt_messages")
        if adapter is None:
            return checker_verdict()
        modalities = ("text", "image") if checker_sees == "pixels" else ("text",)
        checker_messages.append(await adapter(call.messages, modalities))
        return checker_verdict(
            visual_claims            = [{"claim": "1.27.4 修好了", "evidence": detail}],
            visual_scan_complete     = True,
            image_evidence_respected = True,
        )

    model = ScriptedChatModel(
        reply=reply, checker=checker, vision=f"公告写着 {detail}，详情见 {link}"
    )
    scripted_chat_context(mock_context, model, _ALWAYS_CHECK)
    event                             = chat_message_event(sample_group_event, "这个修了吗")
    event["_xc_effective_user_parts"] = (
        {"kind": "text", "text": "这个修了吗"},
        # 入站摘要（有的话）只有笼统描述：细节和链接只在图里。
        {
            "kind"       : "image",
            "description": "一张漏洞公告" if described else "",
            "file_path"  : str(path),
        },
    )
    event["_xc_rendered_media_items"] = [
        RenderedMedia("notice-hash", "image", "漏洞公告", (), summary, cached_path=path)
    ]
    draft = await draft_reply(mock_context, event, text=f"这个修了吗{summary}")

    assert draft is not None and "1.27.4" in draft.text and link in draft.text
    [materials] = [messages[1]["content"] for messages in checker_messages]
    if checker_sees == "pixels":
        assert materials[1]["image_url"]["url"].startswith("data:image/png;base64,")
    else:
        assert detail in materials
    # 同一张图最多转写一次：生成、预检和审查共用。
    assert len(model.calls_of("vision")) <= 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_generate_reply_prefers_planner_goal_over_review_override_goal_store(
    mock_context, sample_group_event
):
    model = ScriptedChatModel(reply="好的", checker=checker_verdict())
    scripted_chat_context(mock_context, model, _ALWAYS_CHECK)
    event = chat_message_event(sample_group_event, "那你推荐哪家？")
    await bound_state(mock_context).goal_store.set_async(
        event_chat_id(event), goal="复盘会话先问感受", source="review"
    )

    draft = await draft_reply(
        mock_context,
        event,
        text           = "那你推荐哪家？",
        reasoning      = "目标: 帮用户选一家合适的火锅店",
        plan_reasoning = "目标: 帮用户选一家合适的火锅店",
    )

    assert draft is not None
    assert draft.text == "好的"
    (checker_call,) = model.calls_of("checker")
    assert "帮用户选一家合适的火锅店" in checker_call.text
    assert "复盘会话先问感受" not in checker_call.text
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_generate_reply_halves_history_after_request_too_large(
    mock_context, sample_group_event
):
    attempts = 0

    def reply_script(_call) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AIError("request_too_large")
        return "好的"

    model = ScriptedChatModel(reply=reply_script)
    scripted_chat_context(
        mock_context, model, {"max_context_size": 4, "reply_check": {"enable_reply_checker": False}}
    )
    event = chat_message_event(sample_group_event, "那你推荐哪家？")
    seed_chat_history(bound_state(mock_context), event_chat_id(event), 4)

    draft = await draft_reply(mock_context, event, text="那你推荐哪家？")

    assert draft is not None
    assert draft.text == "好的"
    first, second = model.calls_of("reply")
    # 请求过大后第二次只带最近两条历史（第 0、1 句被丢掉）。
    assert all(f"第{index}句" in first.text for index in range(4))
    assert all(f"第{index}句" in second.text for index in (2, 3))
    assert not any(f"第{index}句" in second.text for index in (0, 1))
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_generate_reply_prefers_plan_reasoning_goal_when_action_reasoning_has_no_goal(
    mock_context, sample_group_event
):
    model = ScriptedChatModel(reply="好的", checker=checker_verdict())
    scripted_chat_context(mock_context, model, _ALWAYS_CHECK)
    event = chat_message_event(sample_group_event, "那你推荐哪家？")
    await bound_state(mock_context).goal_store.set_async(
        event_chat_id(event), goal="旧目标", source="user"
    )

    draft = await draft_reply(
        mock_context,
        event,
        text           = "那你推荐哪家？",
        reasoning      = "继续回复",
        plan_reasoning = "继续回复\n目标: 帮用户选一家合适的火锅店\n焦点: 预算和口味偏好",
    )

    assert draft is not None
    assert draft.text == "好的"
    (checker_call,) = model.calls_of("checker")
    assert "帮用户选一家合适的火锅店" in checker_call.text
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_generate_reply_draft_exposes_canonical_text_parts(mock_context, sample_group_event):
    model = ScriptedChatModel(reply="第一句。第二句")
    scripted_chat_context(mock_context, model, {"reply_check": {"enable_reply_checker": False}})

    draft = await draft_reply(
        mock_context,
        chat_message_event(sample_group_event, "你好"),
        text      = "你好",
        reasoning = "正常回复",
    )

    assert draft is not None
    # 分段之间以空行分隔，最终发送按空行拆成与分段结果一致的两个气泡。
    assert draft.text == "第一句。\n\n第二句"
    assert draft.text_parts == ("第一句。", "第二句")
    payload = build_reply_payload_from_parts(draft.parts)
    assert len(payload.outbound_batches) == 2
    await settle_chat_background(mock_context, model)
