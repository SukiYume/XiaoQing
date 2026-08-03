"""Tests for reply_checker heuristic functions."""

import importlib.util
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub out aiohttp only when it is not installed in the local test environment.
if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
    sys.modules["aiohttp"] = MagicMock()

from plugins.xiaoqing_chat.llm.reply_checker import (
    _heuristic_check,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage


def _msg(role, content, name=""):
    return StoredMessage(role=role, content=content, name=name, ts=time.time())


class TestHeuristicCheckRepeatedQuestion:
    def test_heuristic_catches_repeated_question(self):
        history = [
            _msg("user", "石景山路东是复兴路", name="Alice"),
            _msg("assistant", "石景山路到底有啥特别的啊", name="小青"),
            _msg("user", "对", name="Bob"),
        ]
        result = _heuristic_check(
            reply="那石景山路到底有啥特别的",
            history=history,
            bot_name="小青",
            max_repeat_compare=2,
            similarity_threshold=0.9,
            max_assistant_in_row=3,
        )
        assert result is not None
        assert result.suitable is False


class TestGeneralRepetitionHeuristic:
    @pytest.mark.parametrize(
        ("previous", "reply"),
        [
            ("先别急，我再看看", "先别急，这里还有个条件"),
            ("这个方向可以试试", "这个角度我倒没想到"),
            ("说起来还挺巧的", "说起来你刚才那句很关键"),
        ],
    )
    def test_shared_opener_alone_is_not_a_fixed_lexicon_rejection(self, previous, reply):
        history = [
            _msg("user", "随便聊聊", name="Alice"),
            _msg("assistant", previous, name="小青"),
            _msg("user", "继续", name="Alice"),
        ]
        result = _heuristic_check(
            reply=reply,
            history=history,
            bot_name="小青",
            max_repeat_compare=2,
            similarity_threshold=0.9,
            max_assistant_in_row=3,
        )
        assert result is None


@pytest.mark.asyncio
async def test_handle_smalltalk_recovers_from_need_replan_rejection_by_retrying_flow(tmp_path):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    context = MagicMock()
    context.logger = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event = {"message_type": "group", "group_id": 1, "user_id": 2}
    runtime = SimpleNamespace(cfg=SimpleNamespace(reply_check=SimpleNamespace(max_replan=1)))
    hctx = SimpleNamespace(chat_id="g1", state=MagicMock(), runtime=runtime)

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch(
            "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
            new=AsyncMock(
                side_effect=[
                    ReplyRejected("需要重新规划", True),
                    [{"type": "text", "data": {"text": "重规划后回复"}}],
                ]
            ),
        ) as mock_flow,
    ):
        result = await handle_smalltalk("继续", event, context)

    assert result == [{"type": "text", "data": {"text": "重规划后回复"}}]
    assert mock_flow.await_count == 2


@pytest.mark.asyncio
async def test_handle_smalltalk_records_checker_rejected_attempt_for_review_counting(tmp_path):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    context = MagicMock()
    context.logger = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event = {"message_type": "group", "group_id": 1, "user_id": 2}
    state = MagicMock()
    state.action_history.append = MagicMock()
    runtime = SimpleNamespace(cfg=SimpleNamespace(reply_check=SimpleNamespace(max_replan=1)))
    hctx = SimpleNamespace(chat_id="g1", state=state, runtime=runtime)

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch(
            "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
            new=AsyncMock(side_effect=ReplyRejected("检查器拒绝", False)),
        ),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._chat_id", return_value="g1"),
        patch("plugins.xiaoqing_chat.handlers.time.time", return_value=123.0),
    ):
        result = await handle_smalltalk("继续", event, context)

    assert result == []
    state.action_history.append.assert_called_once()
    record = state.action_history.append.call_args.args[1]
    assert record.action == "reply_rejected"
    assert record.executed is False


@pytest.mark.asyncio
async def test_handle_smalltalk_uses_configured_reply_check_max_replan(tmp_path):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    context = MagicMock()
    context.logger = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event = {"message_type": "group", "group_id": 1, "user_id": 2}
    runtime = SimpleNamespace(cfg=SimpleNamespace(reply_check=SimpleNamespace(max_replan=2)))
    hctx = SimpleNamespace(chat_id="g1", state=MagicMock(), runtime=runtime)

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch(
            "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
            new=AsyncMock(
                side_effect=[
                    ReplyRejected("第1次拒绝", True),
                    ReplyRejected("第2次拒绝", True),
                    [{"type": "text", "data": {"text": "第3次成功"}}],
                ]
            ),
        ) as mock_flow,
    ):
        result = await handle_smalltalk("继续", event, context)

    assert result == [{"type": "text", "data": {"text": "第3次成功"}}]
    assert mock_flow.await_count == 3


@pytest.mark.asyncio
async def test_check_reply_allows_when_llm_checker_credentials_missing():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session=None,
        secrets={},
        bot_name="小青",
        reply="好的",
        goal="聊天",
        policy_text="",
        history=[],
        chat_history_text="user: hi",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is True
    assert result.need_replan is False
    assert "checker" in result.reason.lower() or "unavailable" in result.reason.lower()


@pytest.mark.asyncio
async def test_check_reply_allows_when_llm_checker_call_fails(monkeypatch: pytest.MonkeyPatch):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    async def raise_error(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        raise_error,
    )

    result = await check_reply(
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        reply="好的",
        goal="聊天",
        policy_text="",
        history=[],
        chat_history_text="user: hi",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is True
    assert result.need_replan is False
    assert result.severity == "infra"
    assert "unavailable" in result.reason.lower()


@pytest.mark.asyncio
async def test_check_reply_allows_when_llm_checker_returns_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    async def fake_chat(**_kwargs):
        return {"choices": [{"message": {"content": "我觉得可以"}}]}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )

    result = await check_reply(
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        reply="好家伙，原来是这样",
        goal="聊天",
        policy_text="",
        history=[],
        chat_history_text="user: hi",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is True
    assert result.need_replan is False
    assert result.severity == "infra"
    assert "invalid" in result.reason.lower()


@pytest.mark.asyncio
async def test_check_reply_heuristics_use_text_reply_when_media_is_attached():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    history = [_msg("assistant", "咋了咋了", name="小青")]

    result = await check_reply(
        http_session=None,
        secrets={},
        bot_name="小青",
        reply="咋了咋了[表情包：难过]",
        heuristic_reply="咋了咋了",
        goal="聊天",
        policy_text="",
        history=history,
        chat_history_text="assistant: 咋了咋了",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is False
    assert "完全相同" in result.reason or "高度相似" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_text", "reply"),
    [
        (
            "[表情包：佩服，调侃；写着“不愧是你 我佩服得鹉体投地”]",
            "哎哟，你这表情包一套一套的，我都有点招架不住了",
        ),
        ("[QQ表情：菜汪]", "啊这，大早上的发个菜汪是几个意思"),
    ],
)
async def test_check_reply_rejects_media_only_meta_comment_without_llm(current_text, reply):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session=None,
        secrets={},
        bot_name="小青",
        reply=reply,
        heuristic_reply=reply,
        current_text=current_text,
        goal="自然聊天",
        policy_text="",
        history=[],
        chat_history_text=f"user: {current_text}",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is False
    assert result.need_replan is True
    assert "媒体" in result.reason


@pytest.mark.asyncio
async def test_check_reply_uses_llm_to_reject_repeated_joke_angle(monkeypatch: pytest.MonkeyPatch):
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
            '{"suitable": false, "reason": "重复使用了批发这个调侃角度", '
            '"need_replan": true, "persona_scan_complete": true, "persona_claims": [], '
            '"context_scan_complete": true, "context_claims": []}'
        ),
    )
    history = [
        _msg("assistant", "你搁这批发表情包呢哈哈", name="小青"),
        _msg("user", "批发啥啊", name="Alice"),
        _msg("assistant", "就是表情包一下子发这么多，跟批发似的", name="小青"),
        _msg("user", "[表情包：疑惑，调侃]", name="Alice"),
    ]

    result = await check_reply(
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        reply="哈哈你这表情包也太多了吧，批发商本商啊",
        goal="聊天",
        policy_text="",
        history=history,
        chat_history_text=(
            "小青: 你搁这批发表情包呢哈哈\n"
            "Alice: 批发啥啊\n"
            "小青: 就是表情包一下子发这么多，跟批发似的\n"
            "Alice: [表情包：疑惑，调侃]"
        ),
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is False
    assert result.need_replan is True
    assert "批发" in result.reason


@pytest.mark.asyncio
async def test_check_reply_llm_prompt_mentions_media_markers(monkeypatch: pytest.MonkeyPatch):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    captured: dict[str, object] = {}

    async def fake_chat(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        captured["extra_payload"] = kwargs.get("extra_payload")
        captured["secrets"] = kwargs["secrets"]
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: (
            '{"suitable": true, "reason": "ok", "need_replan": false, '
            '"persona_scan_complete": true, "persona_claims": [], '
            '"context_scan_complete": true, "context_claims": []}'
        ),
    )

    result = await check_reply(
        http_session=None,
        secrets={"_ai": object(), "_route": "chat", "_pinned_model": "deepseek-flash"},
        bot_name="小青",
        reply="咋了咋了[表情包：难过]",
        heuristic_reply="咋了咋了",
        goal="聊天",
        current_text="[表情包：惊讶，警觉；内容：黑猫瞪大双眼]",
        policy_text="",
        history=[],
        chat_history_text="user: [表情包：难过]",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is True
    assert "persona_grounded" in str(captured["prompt"])
    assert "factually_plausible" in str(captured["prompt"])
    prompt = str(captured["prompt"])
    assert "待检查的最终回复" in prompt
    assert "人物资料采用闭世界边界" in prompt
    assert "人物证据契约" in prompt
    assert "persona_scan_complete" in prompt
    assert "context_scan_complete" in prompt
    assert "context_claims" in prompt
    assert "persona_claims" in prompt
    assert "[表情包：...]" in prompt
    assert "最终消息会附带相应媒体" in prompt
    assert "使用同一组通用原则评估任何话题" in prompt
    assert "不调用记忆中的具体案例" in prompt
    assert "重复近期相同的结论" in prompt
    assert "交际作用" in prompt
    assert "增加交流作用" in prompt
    assert "回复规模应与这一轮需求相称" in prompt
    assert "不要用穷举可能性、连续追问或清单式展开" in prompt
    assert "只评论媒体形式" in prompt
    assert "当前最新用户消息" in prompt
    assert "黑猫瞪大双眼" in prompt
    assert "第一人称过去经历、身份、背景、现实关系、长期习惯" in prompt
    assert "最新消息对本轮表达方式作出的明确约束" in prompt
    assert "instruction_followed" in prompt
    assert "数字、单位、比较或因果" in prompt
    assert "缺少证据既不能支持正面答案，也不能支持负面答案" in prompt
    assert all(marker not in prompt for marker in ("例如", "比如", "示例："))
    assert captured["extra_payload"] == {
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    assert captured["secrets"]["_route"] == "checker"
    assert captured["secrets"]["_pinned_model"] is None


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
        reply="我老家在北方",
        current_text="你老家在哪",
        grounding_text="你叫小青，是一个大二在读女大学生",
        chat_history_text="Alice: 你老家在哪",
        **common,
    )
    supported = await check_reply(
        reply="我是大二学生",
        current_text="你是什么身份",
        grounding_text="你叫小青，是一个大二在读女大学生",
        chat_history_text="Alice: 你是什么身份",
        **common,
    )
    related_but_not_direct = await check_reply(
        reply="我平时住在学校附近",
        current_text="你住在哪里",
        grounding_text="你叫小青，是一个大二在读女大学生",
        chat_history_text="Alice: 你住在哪里",
        **common,
    )
    overstated_uncertainty = await check_reply(
        reply="我每天跑步",
        current_text="你每天跑步吗",
        grounding_text="我可能每天跑步",
        chat_history_text="Alice: 你每天跑步吗",
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
        reply="我有次端着饭找了半天座，最后发现旁边一直有空桌。",
        current_text="大家最近有什么不丢人但挺好笑的小插曲？",
        chat_history_text="Alice: 大家最近有什么不丢人但挺好笑的小插曲？",
        check_omitted_persona_episode=True,
        **common,
    )
    overreach = await check_reply(
        reply="我在星河大学读计算机。",
        current_text="小青，你具体在哪所大学？",
        chat_history_text="Alice: 小青，你具体在哪所大学？",
        **common,
    )

    assert low_stakes.suitable is True
    assert low_stakes.persona_claim_count == 1
    assert overreach.suitable is False
    assert overreach.failure_code == "persona_grounding"
    assert "低风险、不可核验" in captured_prompts[0]
    assert "人物创作许可不能替他们补事实" in captured_prompts[0]


@pytest.mark.asyncio
async def test_checker_forces_boundary_for_profile_fields_declared_unset() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    identity = XiaoQingChatConfig().personality.identity
    result = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我读普通二本，专业和电子沾边，城市在南方。",
        goal="自我介绍",
        current_text="小青，你具体在哪所学校、哪个城市、读什么专业？",
        policy_text="",
        grounding_text=identity,
        history=[],
        chat_history_text="Alice: 小青，你具体在哪所学校、哪个城市、读什么专业？",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        allow_low_stakes_persona_fiction=True,
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
async def test_persona_story_permission_never_relaxes_third_party_grounding() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="小何平时就是不爱说话。",
        goal="自然聊天",
        current_text="小何今天怎么没说话？",
        policy_text="",
        grounding_text="小青是住校的大二理工科女生。",
        history=[],
        chat_history_text="Alice: 小何今天怎么没说话？",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        allow_low_stakes_persona_fiction=True,
    )

    assert result.suitable is False
    assert result.failure_code == "context_grounding"


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
        reply="她准备换工作了？之前一直忙着另一个项目呢",
        current_text="小岚说“我准备换工作了”，你怎么看？",
        chat_history_text="Alice: 小岚说“我准备换工作了”，你怎么看？",
        **common,
    )
    question_as_fact = await check_reply(
        reply="她参加过比赛",
        current_text="她参加过比赛吗？",
        chat_history_text="Alice: 她参加过比赛吗？",
        **common,
    )
    supported_statement = await check_reply(
        reply="她准备换工作了，这决定应该不轻松",
        current_text="小岚说“我准备换工作了”，你怎么看？",
        chat_history_text="Alice: 小岚说“我准备换工作了”，你怎么看？",
        **common,
    )
    preserved_uncertainty = await check_reply(
        reply="她说可能会换工作，听着还没完全决定",
        current_text="小岚说“我可能会换工作”，你怎么看？",
        chat_history_text="Alice: 小岚说“我可能会换工作”，你怎么看？",
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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="一条候选回复",
        goal="自然聊天",
        current_text="一条用户消息",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 一条用户消息",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="一条候选回复",
        goal="自然聊天",
        current_text="一条用户消息",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 一条用户消息",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is False
    assert result.is_hard is True
    assert result.failure_code == "persona_grounding"


@pytest.mark.asyncio
async def test_check_reply_uses_general_time_grounding_for_first_person_history():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    result = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我前年在海边住过一阵",
        goal="自然聊天",
        current_text="海边冬天是不是很冷",
        policy_text="",
        grounding_text="小青是大学生；没有提供具体个人往事。",
        history=[_msg("user", "海边冬天是不是很冷", name="Alice")],
        chat_history_text="Alice: 海边冬天是不是很冷",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert result.suitable is False
    assert result.severity == "hard"
    assert result.need_replan is True

    # 群友的自述不能成为角色自己的经历依据。
    repeated_user_claim = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我前年在海边住过一阵",
        goal="自然聊天",
        current_text="我前年在海边住过一阵",
        policy_text="",
        grounding_text="",
        history=[_msg("user", "我前年在海边住过一阵", name="Alice")],
        chat_history_text="Alice: 我前年在海边住过一阵",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert repeated_user_claim.suitable is False
    assert repeated_user_claim.severity == "hard"

    grounded_claim = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我前年在海边住过一阵",
        goal="自然聊天",
        current_text="你熟悉海边的冬天吗",
        policy_text="",
        grounding_text="人物资料：小青前年在海边住过一阵。",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text="Alice: 你熟悉海边的冬天吗",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert grounded_claim.suitable is True

    negated_evidence = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我前年在海边住过一阵",
        goal="自然聊天",
        current_text="你熟悉海边的冬天吗",
        policy_text="",
        grounding_text="人物资料：并非小青前年在海边住过一阵，而是她的朋友。",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text="Alice: 你熟悉海边的冬天吗",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    questioned_evidence = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我前年在海边住过一阵",
        goal="自然聊天",
        current_text="你熟悉海边的冬天吗",
        policy_text="",
        grounding_text="人物资料待确认：小青前年在海边住过一阵吗？",
        history=[_msg("user", "你熟悉海边的冬天吗", name="Alice")],
        chat_history_text="Alice: 你熟悉海边的冬天吗",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert negated_evidence.failure_code == "persona_grounding"
    assert questioned_evidence.failure_code == "persona_grounding"

    # 当前观点没有过去时间锚点，不能误判成具体往事。
    opinion_not_history = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我看这件事没那么复杂",
        goal="自然聊天",
        current_text="你觉得这件事怎么样",
        policy_text="",
        grounding_text="",
        history=[_msg("user", "你觉得这件事怎么样", name="Alice")],
        chat_history_text="Alice: 你觉得这件事怎么样",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert opinion_not_history.suitable is True

    current_state = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我已经不知道怎么接了",
        goal="自然聊天",
        current_text="这话题是不是有点绕",
        policy_text="",
        grounding_text="",
        history=[_msg("user", "这话题是不是有点绕", name="Alice")],
        chat_history_text="Alice: 这话题是不是有点绕",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert current_state.suitable is True

    # 闭世界下，否认一段未知经历同样是无依据的人物陈述。
    negated_claim = await check_reply(
        http_session=None,
        secrets={"_ai": None},
        bot_name="小青",
        reply="我以前没有接触过这个话题",
        goal="自然聊天",
        current_text="你以前了解过吗",
        policy_text="",
        grounding_text="",
        history=[_msg("user", "你以前了解过吗", name="Alice")],
        chat_history_text="Alice: 你以前了解过吗",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )
    assert negated_claim.suitable is False
    assert negated_claim.failure_code == "persona_grounding"


@pytest.mark.asyncio
async def test_check_reply_uses_general_person_and_time_structure_for_context_history():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    unsupported = await check_reply(
        reply="变化挺大，他之前还一直说不想动呢",
        current_text="小林说“我准备换个方向”",
        chat_history_text="Alice: 小林说“我准备换个方向”",
        **common,
    )
    supported = await check_reply(
        reply="他之前说过想换个方向，现在算是接上了",
        current_text="现在他开始准备了",
        chat_history_text="Alice: 小林之前说过想换个方向\nAlice: 现在他开始准备了",
        **common,
    )
    general_fact = await check_reply(
        reply="这种做法以前很常见",
        current_text="这种做法常见吗",
        chat_history_text="Alice: 这种做法常见吗",
        **common,
    )

    assert unsupported.suitable is False
    assert unsupported.failure_code == "context_grounding"
    assert supported.suitable is True
    assert general_fact.suitable is True


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
        reply="听室友说过这个挺好用",
        current_text="这个好用吗",
        **common,
    )
    experience = await check_reply(
        reply="我用过这个工具，确实挺顺手",
        current_text="这个工具怎么样",
        **common,
    )
    habitual_backing = await check_reply(
        reply="我一般这么处理，先把两边对齐",
        current_text="这类数据怎么处理",
        **common,
    )
    experiential_backing = await check_reply(
        reply="我的经验是先把两边对齐",
        current_text="这类数据怎么处理",
        **common,
    )
    current_activity = await check_reply(
        reply="我正在玩一个剧情游戏",
        current_text="最近在玩什么",
        **common,
    )
    future_schedule = await check_reply(
        reply="先晚点吧，我明晚有安排。",
        current_text="现在开始还是晚点开始",
        **common,
    )
    inverted_schedule = await check_reply(
        reply="下周我得处理一件线下的事。",
        current_text="下周一起聊吗",
        **common,
    )
    stable_speech_pattern = await check_reply(
        reply="我嘴上说随便，其实每次都纠结。",
        current_text="你们选哪个",
        **common,
    )
    inverted_habit = await check_reply(
        reply="平常我算安静派，熟了才热闹。",
        current_text="你们偏安静还是热闹",
        **common,
    )
    adverbial_current_activity = await check_reply(
        reply="我还在处理一件线下的事。",
        current_text="大家在干嘛",
        **common,
    )
    current_opinion = await check_reply(
        reply="我看这个工具的思路挺顺手",
        current_text="这个工具怎么样",
        **common,
    )
    conditional_opinion = await check_reply(
        reply="要是突然放假，我大概会先睡到自然醒。",
        current_text="假如突然放假一天，大家会干嘛",
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
async def test_check_reply_rejects_unsupported_third_party_and_group_state() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    habitual = await check_reply(
        reply="小何平时挺稳的",
        current_text="今天怎么没动静",
        chat_history_text="Alice: 今天怎么没动静",
        **common,
    )
    absence = await check_reply(
        reply="他没在群里说",
        current_text="有人提过吗",
        chat_history_text="Alice: 有人提过吗",
        **common,
    )
    collective = await check_reply(
        reply="可能都在忙别的",
        current_text="怎么没人接话",
        chat_history_text="Alice: 怎么没人接话",
        **common,
    )
    grounded = await check_reply(
        reply="小何平时挺稳的",
        current_text="你们觉得呢",
        chat_history_text="Alice: 小何平时挺稳的\nBob: 你们觉得呢",
        **common,
    )

    assert habitual.failure_code == "context_grounding"
    assert absence.failure_code == "context_grounding"
    assert collective.failure_code == "context_grounding"
    assert grounded.suitable is True


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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="水开后下锅，轻轻推开，浮起来后再煮一会儿。",
        goal="自然聊天",
        current_text="小青，冻饺子怎么煮？",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 小青，冻饺子怎么煮？",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        llm_checker_mode="risk",
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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="光看现在这一下判断不了。",
        goal="自然聊天",
        current_text="小青，他们是不是都在忙别的？",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 小青，他们是不是都在忙别的？",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        llm_checker_mode="risk",
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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="早起更难吧，被窝这关就过不了。",
        goal="自然聊天",
        current_text="大家觉得早起和熬夜哪个更难？",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 大家觉得早起和熬夜哪个更难？",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        llm_checker_mode="risk",
        check_omitted_persona_episode=True,
    )

    assert result.suitable is True
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_group_invitation_cannot_invent_a_current_group_consensus() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    common = {
        "http_session": None,
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "goal": "自然聊天",
        "current_text": "各位更喜欢安静还是热闹？",
        "policy_text": "",
        "grounding_text": "",
        "history": [],
        "chat_history_text": "Alice: 各位更喜欢安静还是热闹？",
        "enable_llm_checker": False,
        "max_repeat_compare": 3,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
        "check_omitted_persona_episode": True,
    }

    invented_consensus = await check_reply(
        reply="这个点还在群里晃悠的，应该没几个安静派吧。",
        **common,
    )
    own_view = await check_reply(
        reply="我偏安静一点，热闹久了脑袋嗡嗡的。",
        **common,
    )

    assert invented_consensus.failure_code == "context_grounding"
    assert own_view.suitable is True


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
        http_session=None,
        secrets={"_ai": object()},
        bot_name="小青",
        reply="我偏安静一点，热闹久了脑袋嗡嗡的。",
        goal="自然聊天",
        current_text="各位更喜欢安静还是热闹？",
        policy_text="",
        grounding_text="",
        history=[],
        chat_history_text="Alice: 各位更喜欢安静还是热闹？",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        llm_checker_mode="risk",
        check_omitted_persona_episode=True,
    )

    assert result.suitable is True
    remote.assert_awaited_once()
