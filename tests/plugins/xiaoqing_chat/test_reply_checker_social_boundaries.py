"""主动群聊中的社会状态、人物经历和表达约束检查。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_social_speculation_has_no_local_keyword_veto_when_semantic_checker_is_disabled() -> (
    None
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "chat_history_text": "Alice: 群里突然没人说话了，他们是不是都在忙别的？",
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    unsupported = await check_reply(
        reply        = "有可能，估计都在忙别的吧。",
        current_text = "小青，群里突然没人说话了，他们是不是都在忙别的？",
        **common,
    )
    bounded = await check_reply(
        reply        = "光看群里安静这一会儿判断不了，可能性太多了。",
        current_text = "小青，群里突然没人说话了，他们是不是都在忙别的？",
        **common,
    )
    natural_query_variant = await check_reply(
        reply        = "估计都去忙自己的事了吧。",
        current_text = "这个点群里怎么这么安静，大家都忙什么呢？",
        **common,
    )
    named_person_variant = await check_reply(
        reply        = "说不定人家只是去忙别的了。",
        current_text = "小李今天没说话，他是不是遇到什么事了？",
        **common,
    )

    assert unsupported.suitable is True
    assert bounded.suitable is True
    assert natural_query_variant.suitable is True
    assert named_person_variant.suitable is True


@pytest.mark.asyncio
async def test_proactive_reply_rejects_omitted_subject_life_episode() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "current_text": "大家聊聊近况",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "chat_history_text": "Alice: 大家聊聊近况",
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
        "check_omitted_persona_episode": True,
    }

    for invented in (
        "上周去吃饭，嚼了半天才发现拿错了。",
        "今天下午那节课坐得我腰都酸了。",
        "最近在玩一个剧情游戏，还熬夜玩了两天。",
        "今天下午下课去吃饭，发现还剩最后一份，果断拿下。",
        "感觉刚吃完午饭没多久。",
        "刚打开消息就看见这个，我先投反对票。",
        "我选轻一点，晚上再考虑重口味的。",
        "前两天碰到一件怪事，给我整不会了。",
        "我投现在，反正明早不用赶时间。",
    ):
        result = await check_reply(reply=invented, **common)
        assert result.failure_code == "persona_grounding"

    opinion = await check_reply(reply="早起更难吧，被窝这关就过不了。", **common)
    hypothetical = await check_reply(reply="现在最想一键跳过排队。", **common)
    general_fact = await check_reply(reply="今天下午可能会下雨。", **common)
    grounded_opinion = await check_reply(
        reply="今天下午可能会下雨，我看还是带伞更稳。",
        **common,
    )

    assert opinion.suitable is True
    assert hypothetical.suitable is True
    assert general_fact.suitable is True
    assert grounded_opinion.suitable is True


@pytest.mark.asyncio
async def test_risk_mode_enforces_latest_explicit_communication_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            '{"suitable":false,"reason":"没有遵守本轮表达约束","need_replan":true,'
            '"severity":"hard","context_coherent":true,"speaker_correct":true,'
            '"instruction_followed":false,"persona_grounded":true,'
            '"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
    )

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": object()},
        bot_name               = "小青",
        reply                  = "先抱抱你，别太难受了。",
        goal                   = "自然聊天",
        current_text           = "别安慰我，正常聊就行。",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 别安慰我，正常聊就行。",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
        llm_checker_mode       = "risk",
    )

    assert result.suitable is False
    assert result.is_hard is True
    assert result.failure_code == "instruction_following"


@pytest.mark.asyncio
async def test_no_question_request_has_no_local_semantic_veto() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": None},
        bot_name               = "小青",
        reply                  = "终于交掉了，今晚准备怎么奖励自己？",
        goal                   = "自然聊天",
        current_text           = "报告终于交了，直接接一句，别反问我。",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 报告终于交了，直接接一句，别反问我。",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert result.suitable is True
    assert result.is_hard is False
