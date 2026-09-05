"""图像事实进入语义审查，图片词和固定告知句式均不决定结论。"""

import json
from unittest.mock import AsyncMock

import pytest

from plugins.xiaoqing_chat.llm import reply_checker as checker
from plugins.xiaoqing_chat.llm.media_evidence import event_has_current_image, image_evidence_block


def _response(**changes):
    payload = {
        "suitable": True,
        "reason": "符合输入",
        "need_replan": False,
        "severity": "soft",
        "context_coherent": True,
        "speaker_correct": True,
        "instruction_followed": True,
        "persona_grounded": True,
        "factually_plausible": True,
        "non_template": True,
        "image_evidence_respected": True,
        "persona_scan_complete": True,
        "persona_claims": [],
        "context_scan_complete": True,
        "context_claims": [],
        "visual_scan_complete": True,
        "visual_claims": [],
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def _options(**changes):
    values = {
        "http_session": None,
        "secrets": {},
        "bot_name": "小青",
        "goal": "回答当前问题",
        "history": [],
        "chat_history_text": "",
        "enable_llm_checker": True,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1,
        "max_retry": 0,
        "retry_interval_seconds": 0,
        "current_image_attached": False,
        "llm_checker_mode": "risk",
    }
    values.update(changes)
    return values


def test_image_evidence_comes_from_structured_attachments():
    assert not event_has_current_image({"message": "看看这张图 [图片：绿色背景]"})
    assert event_has_current_image({"message": [{"type": "image", "data": {}}]})
    assert "历史含实际图像记录" in image_evidence_block(False, history_available=True)
    assert "当前事件未提供图像附件" in image_evidence_block(False)


@pytest.mark.asyncio
async def test_plain_programming_reply_does_not_force_image_disclaimer_or_always_check(monkeypatch):
    remote = AsyncMock()
    monkeypatch.setattr(checker, "_llm_check", remote)
    result = await checker.check_reply(
        reply="可以使用 Pillow 保存为 PNG。", current_text="图片格式转换怎么写", **_options()
    )
    assert result.suitable
    remote.assert_not_awaited()


@pytest.mark.parametrize(
    "image_axis,expected", [(False, "hard"), (None, "infra"), ("true", "infra")]
)
def test_visual_facts_and_malformed_protocol_have_distinct_outcomes(image_axis, expected):
    result = checker._interpret_checker_response(
        content=_response(image_evidence_respected=image_axis),
        reply                            = "UI是绿色配白字。",
        current_text                     = "这张图怎么样",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.severity == expected


@pytest.mark.asyncio
async def test_checker_preserves_generator_context_and_uses_general_evidence_rules(monkeypatch):
    remote = AsyncMock(return_value=({"choices": [{"message": {"content": _response()}}]}, "test"))
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    history   = "历史起点：附件是一张蓝色圆形图。" + "正常交流。" * 250
    current   = "请保留这个最初约束。" + "当前输入。" * 150
    grounding = "资料起点：角色只设定名字。" + "人物资料。" * 350
    result    = await checker._llm_check(
        secrets     = {},
        check_input = checker._LLMCheckInput(
            "小青",
            "按你的描述处理。",
            "帮助",
            current,
            "请保留全文。" * 70,
            grounding,
            history,
            True,
            False,
            True,
        ),
        request_policy=checker._LLMRequestPolicy(1024, 1, 0, 0),
    )
    prompt    = remote.call_args.kwargs["prompt"]
    materials = json.loads(remote.call_args.kwargs["materials"])
    assert materials["最近对话"] == history
    assert materials["当前最新用户消息"] == current
    assert materials["受控人物资料"] == grounding
    assert history not in prompt and current not in prompt
    assert '"suitable":true' not in prompt
    assert "用户邀请的推测可以提出新可能" in prompt
    assert "缺图无需固定告知措辞" in prompt
    assert result.suitable


@pytest.mark.parametrize("reply", ["墙壁偏浅米白，接近暖灰。", "这个配色和表情看着很有趣。"])
def test_unbacked_visual_inventory_overrides_all_true_verdict(reply):
    result = checker._interpret_checker_response(
        content=_response(visual_claims=[{"claim": reply, "evidence": ""}]),
        reply                            = reply,
        current_text                     = "评价一下附件。",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.failure_code == "media_grounding"
    assert result.is_hard


@pytest.mark.parametrize(
    "source", ["用户描述：房间刷了很浅的米白色涂料。", "[图片：房间刷了很浅的米白色涂料。]"]
)
def test_visual_semantic_paraphrase_keeps_real_description_evidence(source):
    reply  = "室内墙面是偏浅的奶油色。"
    result = checker._interpret_checker_response(
        content=_response(
            visual_claims=[{"claim": reply, "evidence": "房间刷了很浅的米白色涂料。"}]
        ),
        reply                            = reply,
        current_text                     = "评价一下。",
        history_text                     = source,
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.suitable


def test_legacy_all_true_payload_without_visual_scan_is_infrastructure_failure():
    payload = json.loads(_response())
    del payload["visual_scan_complete"]
    del payload["visual_claims"]
    result = checker._interpret_checker_response(
        content                          = json.dumps(payload),
        reply                            = "颜色偏浅。",
        current_text                     = "看看截图",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.severity == "infra"


@pytest.mark.asyncio
async def test_invited_speculation_is_decided_by_semantic_checker(monkeypatch):
    remote = AsyncMock(return_value=checker.ReplyCheckResult(True, "已标注推测", False))
    monkeypatch.setattr(checker, "_llm_check", remote)
    result = await checker.check_reply(
        reply        = "可能大家暂时去忙了，这只是猜测。",
        current_text = "请推测群里安静的几个可能原因。",
        **_options(llm_checker_mode="always"),
    )
    assert result.suitable
    remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_checker_unavailability_preserves_infra_classification(monkeypatch):
    monkeypatch.setattr(checker, "_llm_check", AsyncMock(side_effect=TimeoutError))
    result = await checker.check_reply(
        reply="需要对应画面才能确认布局。", current_text="这张截图怎么样", **_options()
    )
    assert result.severity == "infra"
