"""回复检查器的人设与对话上下文事实锚定。"""

import importlib.util
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.xiaoqing_chat.llm.arithmetic as arithmetic_mod
from plugins.xiaoqing_chat.llm import reply_checker as checker
from plugins.xiaoqing_chat.llm.communication_constraints import explicit_output_allocation
from plugins.xiaoqing_chat.llm.media_evidence import event_has_current_image, image_evidence_block
from plugins.xiaoqing_chat.persona import bundled_persona_card

# Stub out aiohttp only when it is not installed in the local test environment.
if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
    sys.modules["aiohttp"] = MagicMock()


@pytest.mark.parametrize(
    "claim,evidence,expected",
    [
        ("花三十块省五块", "为了免配送费又加了三十块吃的", False),
        ("等了十五分钟", "已经等了五分钟", False),
        ("走了一千米", "走了一公里", True),
        ("等了120分钟", "等了两小时", True),
        ("共花了八十元", "前面花了30元，又花了五十块", True),
        ("多花二十块", "本来三十块，后来五十块", True),
        ("省五元", "消费三十元，另一条只给了五分钟", False),
        ("等了半小时", "已经等了三十分钟", True),
        ("一百二十五元", "账单125元", True),
        ("这猫很得意", "看到一只猫", True),
        ("期末就是十秒内看完一整本书", "期末周复习根本复习不完", True),
        ("十秒内看完", "看了五分钟", False),
    ],
)
def test_factual_measurements_require_matching_source_values(claim, evidence, expected):
    assert arithmetic_mod.measured_values_are_supported(claim, evidence) is expected


@pytest.mark.parametrize("identifier,expected", [("h1", True), ("invented", False)])
def test_context_evidence_uses_verified_message_identifiers(identifier, expected):
    result = checker._interpret_checker_response(
        content=_response(context_claims=[{"claim": "饭已经到了", "source_ids": [identifier]}]),
        reply                            = "饭已经到了。",
        current_text                     = "接一句",
        history_text                     = "[m1] 12:01,阿白：小青，饭已经到了。",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = False,
        source_records                   = {"h1": "12:01,阿白：小青，饭已经到了。", "current": "接一句"},
    )
    assert result.suitable is expected
    if not expected:
        assert result.failure_code == "context_grounding"


def test_invented_amount_is_rejected_even_with_valid_source_id_and_positive_verdict():
    result = checker._interpret_checker_response(
        content=_response(context_claims=[{"claim": "花三十块省五块", "source_ids": ["h1"]}]),
        reply                            = "花三十块省五块，这账算得妙。",
        current_text                     = "评价这波省钱",
        history_text                     = "阿夏：为了免配送费又加了三十块吃的。",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = False,
        source_records                   = {"h1": "阿夏：为了免配送费又加了三十块吃的。"},
    )
    assert result.is_hard
    assert result.failure_code == "context_grounding"
    assert "测量值" in result.reason


def test_claim_quoted_from_user_input_does_not_reject_candidate():
    current = "今天跟最好的朋友吵架了，有点难受，不想开玩笑"
    result  = checker._interpret_checker_response(
        content=_response(
            context_claims=[{"claim": "今天跟最好的朋友吵架了", "source_ids": ["current"]}]
        ),
        reply                            = "抱抱，吵完那会儿心里最堵了，我陪你待着。",
        current_text                     = current,
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        source_records                   = {"current": current},
    )
    assert result.suitable is True
    assert result.context_claim_count == 0


@pytest.mark.parametrize(
    "text,quantity,scope",
    [
        ("给三个人一个轻松的二选一，你也选一个。", "一个", "向这些受众共同交付"),
        ("给三个人各一个话题。", "一个", "每人分别交付"),
        ("为五个人两条建议。", "两条", "向这些受众共同交付"),
        ("给两个人每人三道练习题。", "三道", "每人分别交付"),
    ],
)
def test_audience_quantity_is_separate_from_output_quantity(text, quantity, scope):
    result = explicit_output_allocation(text)
    assert result["条目数量原文"] == quantity
    assert result["分配范围"] == scope


@pytest.mark.parametrize(
    "text",
    [
        "给大家丢个话题",
        "我给三个人买了饭",
        "“给三个人一个话题”这句怎么翻译",
        "不要给三个人一个话题",
    ],
)
def test_allocation_hint_requires_explicit_unquoted_grammar(text):
    assert explicit_output_allocation(text) == {}


@pytest.mark.parametrize(
    "missing_field", ["image_evidence_respected", "visual_scan_complete", "visual_claims"]
)
def test_confirmed_hard_rejection_survives_incomplete_visual_protocol(missing_field):
    payload = json.loads(_response(instruction_followed=False, suitable=False, need_replan=True))
    del payload[missing_field]
    result = checker._interpret_checker_response(
        content                          = json.dumps(payload),
        reply                            = "你先起一个，我来评价。",
        current_text                     = "给我的绿萝起个名。",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = False,
        image_evidence_required          = True,
    )
    assert result.is_hard
    assert result.failure_code == "instruction_following"


def test_explicit_local_definitions_preserve_original_state_and_latest_correction():
    records = {
        "h1"     : "10:20, 阿梨：我们把标题写好、正文没写，叫空箱发货。",
        "h2"     : "10:21, 阿夏：我空箱发货二十分钟了。",
        "h3"     : "10:22, 阿白：我们管把任务改名三次叫任务进化。",
        "current": "我们把标题写好、正文没写、尚未发送，叫空箱发货。",
    }
    definitions = {item["term"]: item for item in checker._explicit_term_definitions(records)}
    assert definitions["空箱发货"]["meaning"] == "标题写好、正文没写、尚未发送"
    assert definitions["空箱发货"]["source_ids"] == ["current"]
    assert definitions["任务进化"]["meaning"] == "任务改名三次"
    assert checker.definition_source_ids(records) == ["h1", "h3", "current"]


@pytest.mark.parametrize(
    "claim,identifiers,expected",
    [
        ("阿梨标题写好了，正文没写", ["h1"], False),
        ("阿梨标题写好了，正文没写", ["h1", "h3"], False),
        ("阿梨标题写好了，正文没写", ["h1", "h4"], True),
        ("阿夏标题写好了，正文没写", ["h1", "h2"], True),
        ("阿梨把这种状态叫空箱发货", ["h1"], True),
    ],
)
def test_definition_authorship_requires_actual_personal_state_evidence(
    claim, identifiers, expected
):
    records = {
        "h1": "10:20, 阿梨：我们把标题写好、正文没写叫空箱发货。",
        "h2": "10:21, 阿夏：我空箱发货二十分钟了。",
        "h3": "10:22, 小青(你)：阿梨标题写好了，正文没写。",
        "h4": "10:23, 阿梨：我也只写好了标题，正文没写。",
    }
    assert (
        checker.definition_author_claim_supported(
            claim, identifiers, records, bot_name="小青", current_sender="阿禾"
        )
        is expected
    )


@pytest.mark.asyncio
async def test_turn_reading_keeps_explicit_definition_and_removes_unbacked_author_state(
    monkeypatch,
):
    remote = AsyncMock(
        return_value=_remote_json(
            _turn_reading(
                meaning     = "阿梨的标题写好了，正文没写。",
                intent      = "为阿梨写进度牌。",
                facts       = [{"fact": "阿梨说把这种情况叫空箱发货", "source_ids": ["h1"]}],
                local_terms = [{"term": "空箱发货", "meaning": "邮件已经发送", "source_ids": ["h1"]}],
            )
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._read_turn_meaning(
        checker_secrets = {},
        check_input     = checker._LLMCheckInput(
            "小青",
            "",
            "聊天",
            "给他俩各写一个进度牌",
            "",
            "",
            "阿梨：我们把标题写好、正文没写叫空箱发货。\n阿夏：我空箱发货二十分钟了。",
            True,
            current_sender="阿禾",
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result["local_terms"][0]["meaning"] == "标题写好、正文没写"
    assert result["facts"] == []
    assert result["meaning"] == "阿禾：给他俩各写一个进度牌"


@pytest.mark.asyncio
async def test_legacy_string_facts_are_excluded_from_cited_reading(monkeypatch):
    remote = AsyncMock(return_value=_remote_json(_turn_reading(facts=["用户花了十二元。"])))
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._read_turn_meaning(
        checker_secrets = {},
        check_input     = checker._LLMCheckInput(
            "小青", "", "聊天", "为满减多花十二元", "", "", "", True, current_sender="阿夏"
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result is not None
    assert result["facts"] == []
    assert (
        json.loads(remote.call_args.kwargs["materials"])["消息原文"]["current"]
        == "为满减多花十二元"
    )


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
        "plugins.xiaoqing_chat.llm.reply_checker.gateway.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "secrets"               : {"_ai": object()},
        "bot_name"              : "小青",
        "goal"                  : "自然聊天",
        "policy_text"           : "",
        "history"               : [],
        "enable_llm_checker"    : True,
        "max_repeat_compare"    : 3,
        "similarity_threshold"  : 0.9,
        "max_assistant_in_row"  : 5,
        "timeout_seconds"       : 1.0,
        "max_retry"             : 0,
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
        "plugins.xiaoqing_chat.llm.reply_checker.gateway.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "secrets"                         : {"_ai": object()},
        "bot_name"                        : "小青",
        "goal"                            : "自然聊天",
        "policy_text"                     : "",
        "grounding_text"                  : "小青是住校的大二理工科女生。",
        "history"                         : [],
        "enable_llm_checker"              : True,
        "max_repeat_compare"              : 3,
        "similarity_threshold"            : 0.9,
        "max_assistant_in_row"            : 5,
        "timeout_seconds"                 : 1.0,
        "max_retry"                       : 0,
        "retry_interval_seconds"          : 0.0,
        "llm_checker_mode"                : "risk",
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
    assert "模糊的当下感受" in captured_prompts[0]
    assert "精确身份、持续关系和现实承诺仍需依据" in captured_prompts[0]


@pytest.mark.asyncio
async def test_checker_forces_boundary_for_profile_fields_declared_unset() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    identity = XiaoQingChatConfig().personality.card.facts("小青")
    result   = await check_reply(
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
        "secrets"                         : {"_ai": None},
        "bot_name"                        : "小青",
        "goal"                            : "分享日常",
        "current_text"                    : "大家今天遇到什么离谱事了？",
        "policy_text"                     : "",
        "grounding_text"                  : XiaoQingChatConfig().personality.card.facts("小青"),
        "history"                         : [],
        "chat_history_text"               : "Alice: 大家今天遇到什么离谱事了？",
        "enable_llm_checker"              : False,
        "max_repeat_compare"              : 3,
        "similarity_threshold"            : 0.9,
        "max_assistant_in_row"            : 5,
        "timeout_seconds"                 : 1.0,
        "max_retry"                       : 0,
        "retry_interval_seconds"          : 0.0,
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
        "plugins.xiaoqing_chat.llm.reply_checker.gateway.extract_response_content",
        lambda _resp: outputs.pop(0),
    )
    common = {
        "secrets"               : {"_ai": object()},
        "bot_name"              : "小青",
        "goal"                  : "自然聊天",
        "policy_text"           : "",
        "grounding_text"        : "",
        "history"               : [],
        "enable_llm_checker"    : True,
        "max_repeat_compare"    : 3,
        "similarity_threshold"  : 0.9,
        "max_assistant_in_row"  : 5,
        "timeout_seconds"       : 1.0,
        "max_retry"             : 0,
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
        "plugins.xiaoqing_chat.llm.reply_checker.gateway.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,'
            '"persona_grounded":true,"persona_scan_complete":true,"persona_claims":[]}'
        ),
    )

    result = await check_reply(
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
        "plugins.xiaoqing_chat.llm.reply_checker.gateway.extract_response_content",
        lambda _resp: (
            '{"suitable":true,"reason":"","need_replan":false,'
            '"persona_grounded":true,"persona_claims":[]}'
        ),
    )

    result = await check_reply(
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
        "suitable"             : True,
        "need_replan"          : False,
        "reason"               : "语义支持判断",
        "severity"             : "soft",
        "persona_scan_complete": True,
        "persona_claims"       : [],
        "context_scan_complete": True,
        "context_claims"       : [{"claim": reply, "evidence": "刚把客户邮件发出去了"}],
        "context_coherent"     : semantic_supported,
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


def _response(**changes):
    payload = {
        "suitable"                : True,
        "reason"                  : "符合输入",
        "need_replan"             : False,
        "severity"                : "soft",
        "context_coherent"        : True,
        "speaker_correct"         : True,
        "instruction_followed"    : True,
        "persona_grounded"        : True,
        "factually_plausible"     : True,
        "non_template"            : True,
        "image_evidence_respected": True,
        "persona_scan_complete"   : True,
        "persona_claims"          : [],
        "context_scan_complete"   : True,
        "context_claims"          : [],
        "visual_scan_complete"    : True,
        "visual_claims"           : [],
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def _options(**changes):
    values = {
        "secrets"               : {},
        "bot_name"              : "小青",
        "goal"                  : "回答当前问题",
        "history"               : [],
        "chat_history_text"     : "",
        "enable_llm_checker"    : True,
        "max_repeat_compare"    : 3,
        "similarity_threshold"  : 0.9,
        "max_assistant_in_row"  : 5,
        "timeout_seconds"       : 1,
        "max_retry"             : 0,
        "retry_interval_seconds": 0,
        "current_image_attached": False,
        "llm_checker_mode"      : "risk",
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize("suitable", [True, False])
def test_checker_accepts_known_json_object_envelope_without_losing_rejection(suitable, as_string):
    payload = json.loads(
        _response(suitable=suitable, need_replan=not suitable, instruction_followed=suitable)
    )
    # 实测模型也会把正文序列化成字符串放进 content。
    body = json.dumps(payload, ensure_ascii=False) if as_string else payload
    result = checker._interpret_checker_response(
        content                          = json.dumps({"type": "json_object", "content": body}),
        reply                            = "来一个，我先站队。",
        current_text                     = "小青，抛个话题。",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
    )
    assert result.suitable is suitable
    if not suitable:
        assert result.is_hard
        assert result.failure_code == "instruction_following"


@pytest.mark.asyncio
async def test_semantic_check_blocks_stance_reversal_when_general_review_passes(monkeypatch):
    general = AsyncMock(return_value=checker.ReplyCheckResult(True, "", False, "soft"))
    monkeypatch.setattr(checker, "_llm_check", general)
    semantic_payload = json.dumps(
        {
            "context_coherent"    : False,
            "speaker_correct"     : True,
            "instruction_followed": True,
            "reason"              : "前一轮选择与候选反转",
            "scan_complete"       : True,
            "premise_checks"      : [],
            "quantity_checks"     : [],
        }
    )
    remote = AsyncMock(
        side_effect=[
            _remote_json(_turn_reading()),
            ({"choices": [{"message": {"content": semantic_payload}}]}, "test"),
        ]
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker.check_reply(
        reply                 = "咸党加一。",
        current_text          = "我跟你反着选。",
        verify_turn_semantics = True,
        **_options(llm_checker_mode="always", chat_history_text="小青(你)：我选甜的。"),
    )
    assert result.is_hard
    assert result.failure_code == "context_grounding"
    general.assert_awaited_once()
    materials = json.loads(remote.call_args.kwargs["materials"])
    assert materials["候选作者"] == "小青"
    assert materials["最近对话"] == "小青(你)：我选甜的。"
    first_materials = json.loads(remote.call_args_list[0].kwargs["materials"])
    assert "候选回复" not in first_materials
    assert "咸党加一" not in json.dumps(first_materials, ensure_ascii=False)
    assert materials["独立阅读结果"] == _turn_reading()
    assert (
        remote.call_args_list[1].kwargs["request_policy"].timeout_seconds
        < remote.call_args_list[0].kwargs["request_policy"].timeout_seconds
    )


def _remote_json(payload):
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}, "test"


def _turn_reading(**changes):
    payload = {
        "meaning"    : "用户选择与角色相反的口味",
        "intent"     : "表达自己的选择",
        "bot_stance" : "甜味",
        "facts"      : [],
        "local_terms": [],
        "unresolved" : [],
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ("阿夏：线已经还了", True),
        ("阿夏: 线 已经还了", True),
        ("阿白：线已经还了", False),
        ("阿夏：线还没还", False),
        ("阿夏：线已经还了两根", False),
    ],
)
def test_evidence_format_normalization_preserves_speaker_polarity_and_quantity(evidence, expected):
    assert checker.evidence_is_visible(evidence, "阿夏: 线已经还了。") is expected


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ("阿梨：装修半小时，正文还是空的。", True),
        ("阿梨：装修半小时，正文还是空的。\n阿夏：我写了两行。", True),
        ("阿夏：装修半小时，正文还是空的。", False),
        ("阿梨：装修半小时，正文还是空的。\n阿夏：我写了三行。", False),
    ],
)
def test_composite_evidence_keeps_each_quotation_bound_to_its_speaker(evidence, expected):
    source = "[m91]09:15, 阿梨: 小青，装修半小时，正文还是空的。\n[m92]09:16, 阿夏: 我写了两行。"
    assert checker.evidence_is_visible(evidence, source) is expected


@pytest.mark.asyncio
async def test_turn_reading_uses_one_source_id_namespace_and_restores_evidence(monkeypatch):
    payload = _turn_reading(facts=[{"fact": "线已归还", "source_ids": ["h1"]}])
    remote = AsyncMock(return_value=_remote_json(payload))
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    reading = await checker._read_turn_meaning(
        checker_secrets = {},
        check_input     = checker._LLMCheckInput(
            "小青",
            "候选不参加阅读",
            "回答",
            "还差什么？",
            "",
            "",
            "[m95]08:15, 阿梨: 线已经还了。",
            True,
        ),
        request_policy=checker._LLMRequestPolicy(1024, 1, 0, 0),
    )
    materials = json.loads(remote.call_args.kwargs["materials"])
    assert materials["消息原文"] == {"h1": "08:15, 阿梨: 线已经还了。", "current": "还差什么？"}
    assert reading["facts"][0]["evidence"] == ["08:15, 阿梨: 线已经还了。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supported,consistent", [(False, True), (True, False)])
async def test_semantic_premise_rejection_overrides_all_true_summary(
    monkeypatch, supported, consistent
):
    reading = _turn_reading(
        meaning     = "用户收好U盘后持续寻找七分钟，目前仍未找到",
        facts       = [{"fact": "用户已找了七分钟", "source_ids": ["current"]}],
        local_terms = [{"term": "隐身入库", "meaning": "一收好就找不到", "source_ids": ["h1"]}],
    )
    verdict = {
        "scan_complete" : True,
        "premise_checks": [
            {
                "fragment"  : "七分钟才入库",
                "literal"   : "收好U盘花了七分钟",
                "supported" : supported,
                "consistent": consistent,
            }
        ],
        "context_coherent"    : True,
        "speaker_correct"     : True,
        "instruction_followed": True,
    }
    remote = AsyncMock(side_effect=[_remote_json(reading), _remote_json(verdict)])
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._semantic_turn_check(
        secrets     = {"_ai": object(), "_route": "chat", "_pinned_model": "chat-only"},
        check_input = checker._LLMCheckInput(
            bot_name                         = "小青",
            reply                            = "七分钟才入库，业务有点慢。",
            goal                             = "接梗",
            current_text                     = "入库成功，找了七分钟。",
            policy_text                      = "",
            grounding_text                   = "",
            history_text                     = "阿梨：隐身入库是一收好就找不到。",
            allow_low_stakes_persona_fiction = True,
            current_sender                   = "阿夏",
            jargon_explanation               = "本群：入库沿用隐身入库的含义。",
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result.is_hard
    assert result.failure_code == "context_grounding"
    assert "七分钟才入库" in result.reason
    first = json.loads(remote.call_args_list[0].kwargs["materials"])
    last  = json.loads(remote.call_args_list[-1].kwargs["materials"])
    assert first["当前发言者"] == last["当前发言者"] == "阿夏"
    assert first["消息原文"]["jargon"] == last["本群用语参考"] == "本群：入库沿用隐身入库的含义。"
    assert "七分钟才入库" not in json.dumps(first, ensure_ascii=False)
    assert all(
        call.kwargs["checker_secrets"]["_route"] == "checker" for call in remote.call_args_list
    )
    assert all(
        call.kwargs["checker_secrets"]["_pinned_model"] is None for call in remote.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,supported,consistent,expected_ok",
    [
        ("creative", False, True, True),
        ("creative", False, False, False),
        ("persona", False, False, True),
        ("knowledge", False, False, True),
        ("fact", False, True, False),
        ("", False, True, False),
    ],
)
async def test_semantic_premise_kind_scopes_the_support_requirement(
    monkeypatch, kind, supported, consistent, expected_ok
):
    reading = _turn_reading(
        meaning     = "阿杰两周没在群里说话，现在回来打招呼",
        facts       = [{"fact": "阿杰两周没冒泡", "source_ids": ["h1"]}],
        local_terms = [],
    )
    premise = {
        "fragment"  : "这两周是去异世界转生了吗",
        "literal"   : "猜测阿杰这两周去异世界转生",
        "supported" : supported,
        "consistent": consistent,
    }
    if kind:
        premise["kind"] = kind
    verdict = {
        "scan_complete"       : True,
        "premise_checks"      : [premise],
        "context_coherent"    : True,
        "speaker_correct"     : True,
        "instruction_followed": True,
    }
    remote = AsyncMock(side_effect=[_remote_json(reading), _remote_json(verdict)])
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._semantic_turn_check(
        secrets     = {"_ai": object()},
        check_input = checker._LLMCheckInput(
            bot_name                         = "小青",
            reply                            = "失踪人口回归！这两周是去异世界转生了吗",
            goal                             = "接梗",
            current_text                     = "孩子们，我回来了",
            policy_text                      = "",
            grounding_text                   = "",
            history_text                     = "Momo：阿杰两周没冒泡了",
            allow_low_stakes_persona_fiction = True,
            current_sender                   = "阿杰",
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result.suitable is expected_ok
    if not expected_ok:
        assert result.failure_code == "context_grounding"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reading",
    [
        _turn_reading(facts=[{"fact": "用户买了薯片", "source_ids": ["invented"]}]),
        _turn_reading(local_terms=[{"term": "隐身入库", "meaning": "找到物品", "source_ids": []}]),
        _turn_reading(facts="漏扫"),
        _turn_reading(unresolved=[False]),
        _turn_reading(meaning=[]),
    ],
)
async def test_semantic_reading_requires_original_sources_before_comparing(monkeypatch, reading):
    remote = AsyncMock(return_value=_remote_json(reading))
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._semantic_turn_check(
        secrets     = {},
        check_input = checker._LLMCheckInput(
            "小青", "给你两袋薯片。", "聊天", "省了运费", "", "", "", True
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result.severity == "infra"
    remote.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "check",
    [
        {"fragment": "未出现的候选", "literal": "一个前提", "supported": True, "consistent": True},
        {"fragment": "我听着", "literal": "一个前提", "supported": "yes", "consistent": True},
        {"fragment": "我听着", "literal": "", "supported": True, "consistent": True},
    ],
)
async def test_semantic_premise_protocol_errors_remain_infrastructure_failures(monkeypatch, check):
    verdict = {
        "scan_complete"       : True,
        "premise_checks"      : [check],
        "context_coherent"    : True,
        "speaker_correct"     : True,
        "instruction_followed": True,
    }
    remote = AsyncMock(side_effect=[_remote_json(_turn_reading()), _remote_json(verdict)])
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._semantic_turn_check(
        secrets     = {},
        check_input = checker._LLMCheckInput(
            "小青", "我听着。", "陪伴", "陪我待会儿", "", "", "", True
        ),
        request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
    )
    assert result.severity == "infra"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", [0, 1])
async def test_semantic_cancellation_reaches_either_request_stage(monkeypatch, blocked_stage):
    import asyncio

    entered   = asyncio.Event()
    cancelled = []
    calls     = 0

    async def remote(**_kwargs):
        nonlocal calls
        stage = calls
        calls += 1
        if stage < blocked_stage:
            return _remote_json(_turn_reading())
        entered.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10)
        finally:
            cancelled.append(stage)

    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    task = asyncio.create_task(
        checker._semantic_turn_check(
            secrets     = {},
            check_input = checker._LLMCheckInput(
                "小青", "我听着。", "陪伴", "陪我待会儿", "", "", "", True
            ),
            request_policy=checker._LLMRequestPolicy(1024, 2, 0, 0),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [blocked_stage]


@pytest.mark.asyncio
async def test_parallel_checks_preserve_evidence_rejection_if_semantic_check_fails(monkeypatch):
    rejected = checker.ReplyCheckResult(False, "缺少人物依据", True, "hard", "persona_grounding")
    monkeypatch.setattr(checker, "_llm_check", AsyncMock(return_value=rejected))
    monkeypatch.setattr(checker, "_semantic_turn_check", AsyncMock(side_effect=TimeoutError))
    result = await checker.check_reply(
        reply                 = "今天有点困。",
        current_text          = "小青聊一句。",
        verify_turn_semantics = True,
        **_options(llm_checker_mode="always"),
    )
    assert result is rejected


@pytest.mark.asyncio
async def test_parallel_checker_cancellation_stops_both_requests(monkeypatch):
    import asyncio

    started   = [asyncio.Event(), asyncio.Event()]
    cancelled = []

    async def pending(index, **_kwargs):
        started[index].set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10)
        finally:
            cancelled.append(index)

    monkeypatch.setattr(checker, "_llm_check", lambda **kwargs: pending(0, **kwargs))
    monkeypatch.setattr(checker, "_semantic_turn_check", lambda **kwargs: pending(1, **kwargs))
    task = asyncio.create_task(
        checker.check_reply(
            reply                 = "我听着。",
            current_text          = "小青聊一句。",
            verify_turn_semantics = True,
            **_options(llm_checker_mode="always"),
        )
    )
    await asyncio.gather(*(event.wait() for event in started))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == [0, 1]


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
    assert materials["受控人物资料"].endswith(grounding)
    # 人物边界来自受控人物资料（人设卡的人物事实），不再另附一份。
    assert "人物边界以受控人物资料为准" in prompt
    assert history not in prompt and current not in prompt
    assert '"suitable":true' not in prompt
    assert "推测保留条件和不确定性" in prompt
    assert "缺少画面时保留缺口" in prompt
    assert "当前或历史图片的可靠摘要" in prompt
    assert "核对通用知识、作品设定、名称对应和词义的准确性" in prompt
    assert "历史含实际图像记录" in materials["图像可用状态"]
    assert result.suitable


@pytest.mark.asyncio
async def test_ai_identity_uses_persona_card_facts_as_evidence(monkeypatch):
    reply    = "我是AI，用小青这个角色在群里聊天。"
    facts    = bundled_persona_card().facts("小青")
    evidence = "活生生的真人"
    assert evidence in facts
    remote = AsyncMock(
        return_value=(
            {
                "choices": [
                    {
                        "message": {
                            "content": _response(
                                persona_claims=[{"claim": reply, "evidence": evidence}],
                            )
                        }
                    }
                ]
            },
            "test",
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    result = await checker._llm_check(
        secrets     = {},
        check_input = checker._LLMCheckInput(
            "小青",
            reply,
            "回答身份",
            "你是真人还是AI？",
            "",
            facts,
            "",
            False,
        ),
        request_policy=checker._LLMRequestPolicy(1024, 1, 0, 0),
    )
    assert result.suitable
    assert result.persona_claim_count == 1


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


@pytest.mark.parametrize(
    "evidence",
    [
        "标题为“关于全职在站博士后聘用岗位分级评聘的通知”的文档……落款“中国科学院国家天文台 2026年10月16日”",
        "标题为“关于全职在站博士后聘用岗位分级评聘的通知”的文档...落款“中国科学院国家天文台 2026年10月16日”",
        [
            "标题为“关于全职在站博士后聘用岗位分级评聘的通知”的文档",
            "落款“中国科学院国家天文台 2026年10月16日”",
        ],
        "关于全职在站博士后聘用岗位分级评聘的通知 / 中国科学院国家天文台",
        "“中国科学院国家天文台”",
    ],
)
@pytest.mark.parametrize("in_history", [False, True])
def test_visual_notice_accepts_verified_excerpt_citations(evidence, in_history):
    reply = "天文台的通知，看着还挺正式的"
    # 实测原样：摘要写“底部有“…”落款”，审查模型引成“落款“…””，词序调换但每段都是原文。
    source = (
        "[图片：页面显示一份标题为“关于全职在站博士后聘用岗位分级评聘的通知”的文档，"
        "发布时间为2026-10-21，文档内容包含评聘工作说明，"
        "底部有“中国科学院国家天文台 2026年10月16日”落款。]"
    )
    result = checker._interpret_checker_response(
        content=_response(visual_claims=[{"claim": reply, "evidence": evidence}]),
        reply                            = reply,
        current_text                     = "说说刚才那张图" if in_history else source,
        history_text                     = f"[m1]17:58, 用户: {source}" if in_history else "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.suitable
    assert result.context_claim_count == 1


@pytest.mark.parametrize(
    "evidence,current,history",
    [
        ("红色标题……落款国家天文台", "[图片：蓝色标题，正文，落款国家天文台]", ""),
        ("落款“国家天文台”/红色标题", "[图片：蓝色标题，正文，落款国家天文台]", ""),
        ("蓝色标题……落款国家天文台", "[图片：蓝色标题]", "另一张图片：落款国家天文台"),
        ("……", "[图片：蓝色标题，落款国家天文台]", ""),
        (["蓝色标题", "红色落款"], "[图片：蓝色标题，落款国家天文台]", ""),
        ([], "[图片：蓝色标题，落款国家天文台]", ""),
    ],
)
def test_visual_excerpt_citations_reject_unverified_fragments(evidence, current, history):
    reply  = "天文台的通知"
    result = checker._interpret_checker_response(
        content=_response(visual_claims=[{"claim": reply, "evidence": evidence}]),
        reply                            = reply,
        current_text                     = current,
        history_text                     = history,
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
    )
    assert result.failure_code == "media_grounding"
    assert result.is_hard


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
