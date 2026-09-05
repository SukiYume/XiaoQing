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
            reply                = "那石景山路到底有啥特别的",
            history              = history,
            max_repeat_compare   = 2,
            similarity_threshold = 0.9,
            max_assistant_in_row = 3,
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
            reply                = reply,
            history              = history,
            max_repeat_compare   = 2,
            similarity_threshold = 0.9,
            max_assistant_in_row = 3,
        )
        assert result is None


@pytest.mark.asyncio
async def test_handle_smalltalk_recovers_from_need_replan_rejection_by_retrying_flow(tmp_path):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    context          = MagicMock()
    context.logger   = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event            = {"message_type": "group", "group_id": 1, "user_id": 2}
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

    context                     = MagicMock()
    context.logger              = MagicMock()
    context.data_dir            = tmp_path / "xiaoqing_chat"
    event                       = {"message_type": "group", "group_id": 1, "user_id": 2}
    state                       = MagicMock()
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

    context          = MagicMock()
    context.logger   = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event            = {"message_type": "group", "group_id": 1, "user_id": 2}
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
        http_session           = None,
        secrets                = {},
        bot_name               = "小青",
        reply                  = "好的",
        goal                   = "聊天",
        policy_text            = "",
        history                = [],
        chat_history_text      = "user: hi",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
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
        http_session           = None,
        secrets                = {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name               = "小青",
        reply                  = "好的",
        goal                   = "聊天",
        policy_text            = "",
        history                = [],
        chat_history_text      = "user: hi",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
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
        http_session           = None,
        secrets                = {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name               = "小青",
        reply                  = "好家伙，原来是这样",
        goal                   = "聊天",
        policy_text            = "",
        history                = [],
        chat_history_text      = "user: hi",
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
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
        http_session           = None,
        secrets                = {},
        bot_name               = "小青",
        reply                  = "咋了咋了[表情包：难过]",
        heuristic_reply        = "咋了咋了",
        goal                   = "聊天",
        policy_text            = "",
        history                = history,
        chat_history_text      = "assistant: 咋了咋了",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
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
        http_session           = None,
        secrets                = {},
        bot_name               = "小青",
        reply                  = reply,
        heuristic_reply        = reply,
        current_text           = current_text,
        goal                   = "自然聊天",
        policy_text            = "",
        history                = [],
        chat_history_text      = f"user: {current_text}",
        enable_llm_checker     = False,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
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
        http_session      = None,
        secrets           = {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name          = "小青",
        reply             = "哈哈你这表情包也太多了吧，批发商本商啊",
        goal              = "聊天",
        policy_text       = "",
        history           = history,
        chat_history_text = (
            "小青: 你搁这批发表情包呢哈哈\n"
            "Alice: 批发啥啊\n"
            "小青: 就是表情包一下子发这么多，跟批发似的\n"
            "Alice: [表情包：疑惑，调侃]"
        ),
        enable_llm_checker     = True,
        max_repeat_compare     = 3,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert result.suitable is False
    assert result.need_replan is True
    assert "批发" in result.reason


@pytest.mark.asyncio
async def test_check_reply_llm_prompt_mentions_media_markers(monkeypatch: pytest.MonkeyPatch):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    captured: dict[str, object] = {}

    async def fake_chat(**kwargs):
        captured["prompt"]        = "\n".join(message["content"] for message in kwargs["messages"])
        captured["roles"]         = [message["role"] for message in kwargs["messages"]]
        captured["extra_payload"] = kwargs.get("extra_payload")
        captured["secrets"]       = kwargs["secrets"]
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
    assert captured["roles"] == ["system", "user"]
    assert "persona_grounded" in str(captured["prompt"])
    assert "factually_plausible" in str(captured["prompt"])
    prompt = str(captured["prompt"])
    for required in (
        "待检查回复",
        "受控人物资料",
        "证据契约",
        "persona_scan_complete",
        "context_scan_complete",
        "context_claims",
        "persona_claims",
        "实际附带媒体",
        "当前最新用户消息",
        "黑猫瞪大双眼",
        "instruction_followed",
        "确定事实需要依据",
        "保留来源、否定、条件和不确定性",
    ):
        assert required in prompt
    assert all(marker not in prompt for marker in ("例如", "比如", "示例："))
    assert captured["extra_payload"] == {
        "response_format": {"type": "json_object"},
    }
    assert captured["secrets"]["_route"] == "checker"
    assert captured["secrets"]["_pinned_model"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rejections", [0, 1, 2])
@pytest.mark.parametrize("has_ai", [False, True])
async def test_checker_preserves_route_thinking_during_json_compatibility_fallback(
    monkeypatch, rejections, has_ai
):
    from core.ai import AIRequestError
    from plugins.xiaoqing_chat.llm import reply_checker

    payloads = []

    async def fake_request(**kwargs):
        # 路由默认值由统一 AI 层合并，插件请求始终保持思考配置未覆盖。
        extra = kwargs["extra_payload"]
        payloads.append(extra)
        assert "thinking" not in (extra or {})
        assert "reasoning_effort" not in (extra or {})
        assert kwargs["timeout_seconds"] == (None if has_ai else 30)
        assert kwargs["total_timeout_seconds"] == 30
        if len(payloads) <= rejections:
            raise AIRequestError("request", status=400)
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(reply_checker, "chat_completions_raw_with_fallback_paths", fake_request)
    options = {
        "checker_secrets": {"_route": "checker"},
        "prompt": "审查规则",
        "materials": "{}",
        "max_tokens": 1536,
        "timeout_seconds": 30,
        "max_retry": 0,
        "retry_interval_seconds": 0,
    }
    if has_ai:
        options["checker_secrets"]["_ai"] = object()
    if rejections == 2:
        with pytest.raises(AIRequestError):
            await reply_checker._request_checker_completion(**options)
    else:
        response, _ = await reply_checker._request_checker_completion(**options)
        assert response == {"ok": True}
    assert payloads == [{"response_format": {"type": "json_object"}}] + (
        [None] if rejections else []
    )
