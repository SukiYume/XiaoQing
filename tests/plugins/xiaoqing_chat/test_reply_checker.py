"""回复检查器启发式规则、重规划和基础模型判断。"""

import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub out aiohttp only when it is not installed in the local test environment.
if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
    sys.modules["aiohttp"] = MagicMock()

from plugins.xiaoqing_chat.llm.reply_checker import (
    _heuristic_check,
)
from tests.helpers.reply_checker_test_support import stored_message as _msg


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
