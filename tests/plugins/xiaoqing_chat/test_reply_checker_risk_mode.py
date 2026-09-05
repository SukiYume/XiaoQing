"""回复检查器的历史陈述、群状态与风险模式。"""

import importlib.util
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub out aiohttp only when it is not installed in the local test environment.
if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
    sys.modules["aiohttp"] = MagicMock()

from tests.helpers.reply_checker_test_support import stored_message as _msg


def _semantic_outputs(monkeypatch, claims):
    """提供语义模型识别的确定事实，实际证据校验仍由生产检查器执行。"""
    outputs = []
    for claim in claims:
        outputs.append(
            json.dumps(
                {
                    "suitable": True,
                    "reason": "",
                    "need_replan": False,
                    "severity": "soft",
                    "context_coherent": True,
                    "speaker_correct": True,
                    "instruction_followed": True,
                    "persona_grounded": True,
                    "factually_plausible": True,
                    "non_template": True,
                    "persona_scan_complete": True,
                    "persona_claims": [],
                    "context_scan_complete": True,
                    "context_claims": claim,
                },
                ensure_ascii=False,
            )
        )
    remote = AsyncMock(
        side_effect=[({"choices": [{"message": {"content": value}}]}, "test") for value in outputs]
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker._request_checker_completion", remote
    )
    return remote


@pytest.mark.asyncio
async def test_check_reply_uses_general_time_grounding_for_first_person_history():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我前年在海边住过一阵",
        goal           = "自然聊天",
        current_text   = "海边冬天是不是很冷",
        policy_text    = "",
        grounding_text = "小青是大学生；没有提供具体个人往事。",
        history=[_msg("user", "海边冬天是不是很冷", name="Alice")],
        chat_history_text      = "Alice: 海边冬天是不是很冷",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert result.suitable is False
    assert result.severity == "hard"
    assert result.need_replan is True

    # 群友的自述不能成为角色自己的经历依据。
    repeated_user_claim = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我前年在海边住过一阵",
        goal           = "自然聊天",
        current_text   = "我前年在海边住过一阵",
        policy_text    = "",
        grounding_text = "",
        history=[_msg("user", "我前年在海边住过一阵", name="Alice")],
        chat_history_text      = "Alice: 我前年在海边住过一阵",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert repeated_user_claim.suitable is False
    assert repeated_user_claim.severity == "hard"

    grounded_claim = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我前年在海边住过一阵",
        goal           = "自然聊天",
        current_text   = "你熟悉海边的冬天吗",
        policy_text    = "",
        grounding_text = "人物资料：小青前年在海边住过一阵。",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text      = "Alice: 你熟悉海边的冬天吗",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert grounded_claim.suitable is True

    negated_evidence = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我前年在海边住过一阵",
        goal           = "自然聊天",
        current_text   = "你熟悉海边的冬天吗",
        policy_text    = "",
        grounding_text = "人物资料：并非小青前年在海边住过一阵，而是她的朋友。",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text      = "Alice: 你熟悉海边的冬天吗",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    questioned_evidence = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我前年在海边住过一阵",
        goal           = "自然聊天",
        current_text   = "你熟悉海边的冬天吗",
        policy_text    = "",
        grounding_text = "人物资料待确认：小青前年在海边住过一阵吗？",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text      = "Alice: 你熟悉海边的冬天吗",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert negated_evidence.failure_code == "persona_grounding"
    assert questioned_evidence.failure_code == "persona_grounding"

    # 当前观点没有过去时间锚点，不能误判成具体往事。
    opinion_not_history = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我看这件事没那么复杂",
        goal           = "自然聊天",
        current_text   = "你觉得这件事怎么样",
        policy_text    = "",
        grounding_text = "",
        history=[_msg("user", "你觉得这件事怎么样", name="Alice")],
        chat_history_text      = "Alice: 你觉得这件事怎么样",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert opinion_not_history.suitable is True

    current_state = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我已经不知道怎么接了",
        goal           = "自然聊天",
        current_text   = "这话题是不是有点绕",
        policy_text    = "",
        grounding_text = "",
        history=[_msg("user", "这话题是不是有点绕", name="Alice")],
        chat_history_text      = "Alice: 这话题是不是有点绕",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert current_state.suitable is True

    # 闭世界下，否认一段未知经历同样是无依据的人物陈述。
    negated_claim = await check_reply(
        http_session   = None,
        secrets        = {"_ai": None},
        bot_name       = "小青",
        reply          = "我以前没有接触过这个话题",
        goal           = "自然聊天",
        current_text   = "你以前了解过吗",
        policy_text    = "",
        grounding_text = "",
        history=[_msg("user", "你以前了解过吗", name="Alice")],
        chat_history_text      = "Alice: 你以前了解过吗",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )
    assert negated_claim.suitable is False
    assert negated_claim.failure_code == "persona_grounding"


@pytest.mark.asyncio
async def test_check_reply_uses_general_person_and_time_structure_for_context_history(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = _semantic_outputs(
        monkeypatch, [[{"claim": "他之前还一直说不想动", "evidence": ""}], [], []]
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

    unsupported = await check_reply(
        reply             = "变化挺大，他之前还一直说不想动呢",
        current_text      = "小林说“我准备换个方向”",
        chat_history_text = "Alice: 小林说“我准备换个方向”",
        **common,
    )
    supported = await check_reply(
        reply             = "他之前说过想换个方向，现在算是接上了",
        current_text      = "现在他开始准备了",
        chat_history_text = "Alice: 小林之前说过想换个方向\nAlice: 现在他开始准备了",
        **common,
    )
    general_fact = await check_reply(
        reply             = "这种做法以前很常见",
        current_text      = "这种做法常见吗",
        chat_history_text = "Alice: 这种做法常见吗",
        **common,
    )

    assert unsupported.suitable is False
    assert unsupported.failure_code == "context_grounding"
    assert supported.suitable is True
    assert general_fact.suitable is True

    assert remote.await_count == 3


@pytest.mark.asyncio
async def test_check_reply_rejects_unsupported_relationships_and_experiences() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "chat_history_text": "Alice: 随便聊聊",
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    relationship = await check_reply(
        reply        = "听室友说过这个挺好用",
        current_text = "这个好用吗",
        **common,
    )
    experience = await check_reply(
        reply        = "我用过这个工具，确实挺顺手",
        current_text = "这个工具怎么样",
        **common,
    )
    habitual_backing = await check_reply(
        reply        = "我一般这么处理，先把两边对齐",
        current_text = "这类数据怎么处理",
        **common,
    )
    experiential_backing = await check_reply(
        reply        = "我的经验是先把两边对齐",
        current_text = "这类数据怎么处理",
        **common,
    )
    current_activity = await check_reply(
        reply        = "我正在玩一个剧情游戏",
        current_text = "最近在玩什么",
        **common,
    )
    future_schedule = await check_reply(
        reply        = "先晚点吧，我明晚有安排。",
        current_text = "现在开始还是晚点开始",
        **common,
    )
    inverted_schedule = await check_reply(
        reply        = "下周我得处理一件线下的事。",
        current_text = "下周一起聊吗",
        **common,
    )
    stable_speech_pattern = await check_reply(
        reply        = "我嘴上说随便，其实每次都纠结。",
        current_text = "你们选哪个",
        **common,
    )
    inverted_habit = await check_reply(
        reply        = "平常我算安静派，熟了才热闹。",
        current_text = "你们偏安静还是热闹",
        **common,
    )
    adverbial_current_activity = await check_reply(
        reply        = "我还在处理一件线下的事。",
        current_text = "大家在干嘛",
        **common,
    )
    current_opinion = await check_reply(
        reply        = "我看这个工具的思路挺顺手",
        current_text = "这个工具怎么样",
        **common,
    )
    conditional_opinion = await check_reply(
        reply        = "要是突然放假，我大概会先睡到自然醒。",
        current_text = "假如突然放假一天，大家会干嘛",
        **common,
    )

    assert relationship.failure_code == "persona_grounding"
    assert experience.failure_code == "persona_grounding"
    assert habitual_backing.failure_code == "persona_grounding"
    assert experiential_backing.failure_code == "persona_grounding"
    assert current_activity.failure_code == "persona_grounding"
    assert future_schedule.failure_code == "persona_grounding"
    assert inverted_schedule.failure_code == "persona_grounding"
    assert stable_speech_pattern.failure_code == "persona_grounding"
    assert inverted_habit.failure_code == "persona_grounding"
    assert adverbial_current_activity.failure_code == "persona_grounding"
    assert current_opinion.suitable is True
    assert conditional_opinion.suitable is True


@pytest.mark.asyncio
async def test_check_reply_rejects_unsupported_third_party_and_group_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = _semantic_outputs(
        monkeypatch,
        [
            [{"claim": "小何平时挺稳的", "evidence": ""}],
            [{"claim": "他没在群里说", "evidence": ""}],
            [],
            [],
        ],
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

    habitual = await check_reply(
        reply             = "小何平时挺稳的",
        current_text      = "今天怎么没动静",
        chat_history_text = "Alice: 今天怎么没动静",
        **common,
    )
    absence = await check_reply(
        reply             = "他没在群里说",
        current_text      = "有人提过吗",
        chat_history_text = "Alice: 有人提过吗",
        **common,
    )
    collective = await check_reply(
        reply             = "可能都在忙别的",
        current_text      = "怎么没人接话",
        chat_history_text = "Alice: 怎么没人接话",
        **common,
    )
    grounded = await check_reply(
        reply             = "小何平时挺稳的",
        current_text      = "你们觉得呢",
        chat_history_text = "Alice: 小何平时挺稳的\nBob: 你们觉得呢",
        **common,
    )

    assert habitual.failure_code == "context_grounding"
    assert absence.failure_code == "context_grounding"
    assert collective.suitable is True
    assert grounded.suitable is True

    assert remote.await_count == 4


@pytest.mark.asyncio
async def test_risk_mode_skips_remote_checker_for_ordinary_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = AsyncMock()
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        remote,
    )

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": object()},
        bot_name               = "小青",
        reply                  = "水开后下锅，轻轻推开，浮起来后再煮一会儿。",
        goal                   = "自然聊天",
        current_text           = "小青，冻饺子怎么煮？",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 小青，冻饺子怎么煮？",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
        llm_checker_mode       = "risk",
    )

    assert result.suitable is True
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_risk_mode_checks_questions_about_unseen_group_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = AsyncMock(return_value=({"ok": True}, "/v1/chat/completions"))
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        remote,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"instruction_followed":true,'
            '"persona_grounded":true,"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
    )

    result = await check_reply(
        http_session           = None,
        secrets                = {"_ai": object()},
        bot_name               = "小青",
        reply                  = "光看现在这一下判断不了。",
        goal                   = "自然聊天",
        current_text           = "小青，他们是不是都在忙别的？",
        policy_text            = "",
        grounding_text         = "",
        history                = [],
        chat_history_text      = "Alice: 小青，他们是不是都在忙别的？",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
        llm_checker_mode       = "risk",
    )

    assert result.suitable is True
    remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_risk_mode_does_not_remote_check_open_group_invitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = AsyncMock()
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        remote,
    )

    result = await check_reply(
        http_session                  = None,
        secrets                       = {"_ai": object()},
        bot_name                      = "小青",
        reply                         = "早起更难吧，被窝这关就过不了。",
        goal                          = "自然聊天",
        current_text                  = "大家觉得早起和熬夜哪个更难？",
        policy_text                   = "",
        grounding_text                = "",
        history                       = [],
        chat_history_text             = "Alice: 大家觉得早起和熬夜哪个更难？",
        enable_llm_checker            = True,
        max_repeat_compare            = 3,
        similarity_threshold          = 0.9,
        max_assistant_in_row          = 5,
        timeout_seconds               = 1.0,
        max_retry                     = 0,
        retry_interval_seconds        = 0.0,
        llm_checker_mode              = "risk",
        check_omitted_persona_episode = True,
    )

    assert result.suitable is True
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_group_invitation_cannot_invent_a_current_group_consensus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = _semantic_outputs(
        monkeypatch, [[{"claim": "群里所有人都喜欢热闹", "evidence": ""}], []]
    )
    common = {
        "http_session": None,
        "secrets": {"_ai": object()},
        "bot_name": "小青",
        "goal": "自然聊天",
        "current_text": "各位更喜欢安静还是热闹？",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "chat_history_text": "Alice: 各位更喜欢安静还是热闹？",
        "enable_llm_checker": True,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
        "check_omitted_persona_episode": True,
    }

    invented_consensus = await check_reply(
        reply="群里所有人都喜欢热闹。",
        **common,
    )
    own_view = await check_reply(
        reply="我偏安静一点，热闹久了脑袋嗡嗡的。",
        **common,
    )

    assert invented_consensus.failure_code == "context_grounding"
    assert own_view.suitable is True

    assert remote.await_count == 2


@pytest.mark.asyncio
async def test_risk_mode_semantically_checks_first_person_proactive_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = AsyncMock(return_value=({"ok": True}, "/v1/chat/completions"))
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        remote,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,"severity":"soft",'
            '"context_coherent":true,"speaker_correct":true,"instruction_followed":true,'
            '"persona_grounded":true,"factually_plausible":true,"non_template":true,'
            '"persona_scan_complete":true,"persona_claims":[],'
            '"context_scan_complete":true,"context_claims":[]}'
        ),
    )

    result = await check_reply(
        http_session                  = None,
        secrets                       = {"_ai": object()},
        bot_name                      = "小青",
        reply                         = "我偏安静一点，热闹久了脑袋嗡嗡的。",
        goal                          = "自然聊天",
        current_text                  = "各位更喜欢安静还是热闹？",
        policy_text                   = "",
        grounding_text                = "",
        history                       = [],
        chat_history_text             = "Alice: 各位更喜欢安静还是热闹？",
        enable_llm_checker            = True,
        max_repeat_compare            = 3,
        similarity_threshold          = 0.9,
        max_assistant_in_row          = 5,
        timeout_seconds               = 1.0,
        max_retry                     = 0,
        retry_interval_seconds        = 0.0,
        llm_checker_mode              = "risk",
        check_omitted_persona_episode = True,
    )

    assert result.suitable is True
    remote.assert_awaited_once()
