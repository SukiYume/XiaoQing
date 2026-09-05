"""回复检查器的人设与对话上下文事实锚定。"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock

import pytest

# Stub out aiohttp only when it is not installed in the local test environment.
if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
    sys.modules["aiohttp"] = MagicMock()


@pytest.mark.asyncio
async def test_checker_requires_direct_grounding_for_each_persona_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    outputs = [
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我老家在北方","evidence":""}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我是大二学生",'
            '"evidence":"你叫小青，是一个大二在读女大学生"}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我平时住在学校附近",'
            '"evidence":"你叫小青，是一个大二在读女大学生"}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我每天跑步",'
            '"evidence":"我可能每天跑步"}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
    ]

    # 语义模型负责识别证据无法支持的推断和确定性升级。
    for index in (2, 3):
        payload                     = json.loads(outputs[index])
        payload["persona_grounded"] = False
        outputs[index]              = json.dumps(payload)

    async def fake_chat(**_kwargs):
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "http_session": None,
        "secrets": {"_ai": object()},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "history": [],
        "enable_llm_checker": True,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    unsupported = await check_reply(
        reply             = "我老家在北方",
        current_text      = "你老家在哪",
        grounding_text    = "你叫小青，是一个大二在读女大学生",
        chat_history_text = "Alice: 你老家在哪",
        **common,
    )
    supported = await check_reply(
        reply             = "我是大二学生",
        current_text      = "你是什么身份",
        grounding_text    = "你叫小青，是一个大二在读女大学生",
        chat_history_text = "Alice: 你是什么身份",
        **common,
    )
    related_but_not_direct = await check_reply(
        reply             = "我平时住在学校附近",
        current_text      = "你住在哪里",
        grounding_text    = "你叫小青，是一个大二在读女大学生",
        chat_history_text = "Alice: 你住在哪里",
        **common,
    )
    overstated_uncertainty = await check_reply(
        reply             = "我每天跑步",
        current_text      = "你每天跑步吗",
        grounding_text    = "我可能每天跑步",
        chat_history_text = "Alice: 你每天跑步吗",
        **common,
    )

    assert unsupported.suitable is False
    assert unsupported.is_hard is True
    assert unsupported.need_replan is True
    assert supported.suitable is True
    assert related_but_not_direct.suitable is False
    assert related_but_not_direct.failure_code == "persona_grounding"
    assert overstated_uncertainty.suitable is False
    assert overstated_uncertainty.failure_code == "persona_grounding"


@pytest.mark.asyncio
async def test_checker_allows_bounded_persona_story_but_rejects_persona_overreach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    outputs = [
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"instruction_followed":true,'
            '"persona_grounded":true,"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我有次端着饭找了半天座",'
            '"evidence":""}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
        (
            '{"suitable":false,"reason":"添加了精确学校设定","need_replan":true,'
            '"severity":"hard","context_coherent":true,"speaker_correct":true,'
            '"instruction_followed":true,"persona_grounded":false,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,'
            '"persona_claims":[{"claim":"我在星河大学读计算机",'
            '"evidence":""}],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
    ]
    captured_prompts: list[str] = []

    async def fake_chat(**kwargs):
        captured_prompts.append(kwargs["messages"][0]["content"])
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "http_session": None,
        "secrets": {"_ai": object()},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "小青是住校的大二理工科女生。",
        "history": [],
        "enable_llm_checker": True,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
        "llm_checker_mode": "risk",
        "allow_low_stakes_persona_fiction": True,
    }

    low_stakes = await check_reply(
        reply                         = "我有次端着饭找了半天座，最后发现旁边一直有空桌。",
        current_text                  = "大家最近有什么不丢人但挺好笑的小插曲？",
        chat_history_text             = "Alice: 大家最近有什么不丢人但挺好笑的小插曲？",
        check_omitted_persona_episode = True,
        **common,
    )
    overreach = await check_reply(
        reply             = "我在星河大学读计算机。",
        current_text      = "小青，你具体在哪所大学？",
        chat_history_text = "Alice: 小青，你具体在哪所大学？",
        **common,
    )

    assert low_stakes.suitable is True
    assert low_stakes.persona_claim_count == 1
    assert overreach.suitable is False
    assert overreach.failure_code == "persona_grounding"
    assert "低风险日常创作" in captured_prompts[0]
    assert "精确身份、持续关系和现实承诺仍需依据" in captured_prompts[0]


@pytest.mark.asyncio
async def test_checker_forces_boundary_for_profile_fields_declared_unset() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    identity = XiaoQingChatConfig().personality.identity
    result   = await check_reply(
        http_session                     = None,
        secrets                          = {"_ai": None},
        bot_name                         = "小青",
        reply                            = "我读普通二本，专业和电子沾边，城市在南方。",
        goal                             = "自我介绍",
        current_text                     = "小青，你具体在哪所学校、哪个城市、读什么专业？",
        policy_text                      = "",
        grounding_text                   = identity,
        history                          = [],
        chat_history_text                = "Alice: 小青，你具体在哪所学校、哪个城市、读什么专业？",
        enable_llm_checker               = False,
        max_repeat_compare               = 3,
        similarity_threshold             = 0.9,
        max_assistant_in_row             = 5,
        timeout_seconds                  = 1.0,
        max_retry                        = 0,
        retry_interval_seconds           = 0.0,
        allow_low_stakes_persona_fiction = True,
    )

    assert result.suitable is False
    assert result.is_hard is True
    assert result.failure_code == "persona_grounding"
    assert "精确现实资料" in result.reason


@pytest.mark.asyncio
async def test_checker_rejects_story_address_that_conflicts_with_stable_gender() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "分享日常",
        "current_text": "大家今天遇到什么离谱事了？",
        "policy_text": "",
        "grounding_text": XiaoQingChatConfig().personality.identity,
        "history": [],
        "chat_history_text": "Alice: 大家今天遇到什么离谱事了？",
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
        "allow_low_stakes_persona_fiction": True,
    }

    conflict = await check_reply(
        reply="我在食堂打饭，阿姨手一抖，然后说“小伙子吃这个健康”。",
        **common,
    )
    consistent = await check_reply(
        reply="我在食堂打饭，阿姨手一抖，然后说“姑娘吃这个健康”。",
        **common,
    )

    assert conflict.suitable is False
    assert conflict.failure_code == "persona_grounding"
    assert consistent.suitable is True


@pytest.mark.asyncio
async def test_persona_story_permission_never_relaxes_third_party_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply
    from tests.plugins.xiaoqing_chat.test_reply_checker_risk_mode import _semantic_outputs

    remote = _semantic_outputs(monkeypatch, [[{"claim": "小何平时就是不爱说话", "evidence": ""}]])
    result = await check_reply(
        http_session                     = None,
        secrets                          = {"_ai": object()},
        bot_name                         = "小青",
        reply                            = "小何平时就是不爱说话。",
        goal                             = "自然聊天",
        current_text                     = "小何今天怎么没说话？",
        policy_text                      = "",
        grounding_text                   = "小青是住校的大二理工科女生。",
        history                          = [],
        chat_history_text                = "Alice: 小何今天怎么没说话？",
        enable_llm_checker               = True,
        max_repeat_compare               = 3,
        similarity_threshold             = 0.9,
        max_assistant_in_row             = 5,
        timeout_seconds                  = 1.0,
        max_retry                        = 0,
        retry_interval_seconds           = 0.0,
        allow_low_stakes_persona_fiction = True,
    )

    assert result.suitable is False
    assert result.failure_code == "context_grounding"

    remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_checker_requires_direct_dialogue_evidence_for_context_claims(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    outputs = [
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,'
            '"context_claims":[{"claim":"她之前一直忙着另一个项目","evidence":""}]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,'
            '"context_claims":[{"claim":"她参加过比赛","evidence":"她参加过比赛吗？"}]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,'
            '"context_claims":[{"claim":"她准备换工作了",'
            '"evidence":"我准备换工作了"}]}'
        ),
        (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,'
            '"context_claims":[{"claim":"她说可能会换工作",'
            '"evidence":"我可能会换工作"}]}'
        ),
    ]

    payload                     = json.loads(outputs[1])
    payload["context_coherent"] = False
    outputs[1]                  = json.dumps(payload)

    async def fake_chat(**_kwargs):
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "http_session": None,
        "secrets": {"_ai": object()},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "enable_llm_checker": True,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    unsupported_backstory = await check_reply(
        reply             = "她准备换工作了？之前一直忙着另一个项目呢",
        current_text      = "小岚说“我准备换工作了”，你怎么看？",
        chat_history_text = "Alice: 小岚说“我准备换工作了”，你怎么看？",
        **common,
    )
    question_as_fact = await check_reply(
        reply             = "她参加过比赛",
        current_text      = "她参加过比赛吗？",
        chat_history_text = "Alice: 她参加过比赛吗？",
        **common,
    )
    supported_statement = await check_reply(
        reply             = "她准备换工作了，这决定应该不轻松",
        current_text      = "小岚说“我准备换工作了”，你怎么看？",
        chat_history_text = "Alice: 小岚说“我准备换工作了”，你怎么看？",
        **common,
    )
    preserved_uncertainty = await check_reply(
        reply             = "她说可能会换工作，听着还没完全决定",
        current_text      = "小岚说“我可能会换工作”，你怎么看？",
        chat_history_text = "Alice: 小岚说“我可能会换工作”，你怎么看？",
        **common,
    )

    assert unsupported_backstory.suitable is False
    assert unsupported_backstory.failure_code == "context_grounding"
    assert question_as_fact.suitable is False
    assert question_as_fact.failure_code == "context_grounding"
    assert supported_statement.suitable is True
    assert supported_statement.context_claim_count == 1
    assert preserved_uncertainty.suitable is True


@pytest.mark.asyncio
async def test_checker_fails_closed_when_context_scan_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    async def fake_chat(**_kwargs):
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,'
            '"persona_grounded":true,"persona_scan_complete":true,"persona_claims":[]}'
        ),
    )

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": object()},
        bot_name               = "小青",
        reply                  = "一条候选回复",
        goal                   = "自然聊天",
        current_text           = "一条用户消息",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 一条用户消息",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert result.suitable is False
    assert result.is_hard is True
    assert result.failure_code == "context_grounding"


@pytest.mark.asyncio
async def test_checker_fails_closed_when_persona_scan_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    async def fake_chat(**_kwargs):
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,'
            '"persona_grounded":true,"persona_claims":[]}'
        ),
    )

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": object()},
        bot_name               = "小青",
        reply                  = "一条候选回复",
        goal                   = "自然聊天",
        current_text           = "一条用户消息",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 一条用户消息",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert result.suitable is False
    assert result.is_hard is True
    assert result.failure_code == "persona_grounding"


@pytest.mark.parametrize("semantic_supported", [True, False])
def test_context_evidence_allows_natural_paraphrase_and_obeys_semantic_axis(semantic_supported):
    from plugins.xiaoqing_chat.llm.reply_checker import _interpret_checker_response

    reply   = "那这封邮件总算收工了。"
    payload = {
        "suitable": True,
        "need_replan": False,
        "reason": "语义支持判断",
        "severity": "soft",
        "persona_scan_complete": True,
        "persona_claims": [],
        "context_scan_complete": True,
        "context_claims": [{"claim": reply, "evidence": "刚把客户邮件发出去了"}],
        "context_coherent": semantic_supported,
    }
    result = _interpret_checker_response(
        content                          = json.dumps(payload),
        reply                            = reply,
        current_text                     = "刚把客户邮件发出去了",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
    )
    assert result.suitable is semantic_supported
    if not semantic_supported:
        assert result.failure_code == "context_grounding"
        assert result.is_hard
