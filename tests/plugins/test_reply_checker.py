"""Tests for reply_checker heuristic functions."""
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub out aiohttp before any plugin import that transitively requires it
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = MagicMock()

from plugins.xiaoqing_chat.llm.reply_checker import (
    _check_repeated_question,
    _heuristic_check,
    _is_question_sentence,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage


def _msg(role, content, name=""):
    return StoredMessage(role=role, content=content, name=name, ts=time.time())


class TestIsQuestionSentence:
    def test_question_mark_is_question(self):
        assert _is_question_sentence("石景山路有啥特别的？") is True

    def test_sha_keyword_is_question(self):
        assert _is_question_sentence("石景山路到底有啥特别的啊") is True

    def test_shui_keyword_is_question(self):
        assert _is_question_sentence("松松是谁啊") is True

    def test_plain_statement_not_question(self):
        assert _is_question_sentence("好啊随便") is False

    def test_empty_not_question(self):
        assert _is_question_sentence("") is False


class TestCheckRepeatedQuestion:
    def test_repeated_similar_question_detected(self):
        history = [
            _msg("user", "复兴路", name="Alice"),
            _msg("assistant", "所以石景山路到底有啥特别的啊", name="小青"),
            _msg("user", "对啊", name="Bob"),
        ]
        result = _check_repeated_question(
            reply="石景山路到底有啥特别的",
            bot_msgs=["所以石景山路到底有啥特别的啊"],
        )
        assert result is not None
        assert result.suitable is False

    def test_different_question_allowed(self):
        history = [
            _msg("assistant", "松松是谁啊", name="小青"),
        ]
        result = _check_repeated_question(
            reply="今天天气咋样",
            bot_msgs=["松松是谁啊"],
        )
        assert result is None

    def test_non_question_reply_skipped(self):
        history = [
            _msg("assistant", "石景山路有啥特别的", name="小青"),
        ]
        result = _check_repeated_question(
            reply="哦这样啊",
            bot_msgs=["石景山路有啥特别的"],
        )
        assert result is None

    def test_no_history_allowed(self):
        result = _check_repeated_question(
            reply="松松是谁啊",
            bot_msgs=[],
        )
        assert result is None


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


@pytest.mark.asyncio
async def test_handle_smalltalk_recovers_from_need_replan_rejection_by_retrying_flow(tmp_path):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    context = MagicMock()
    context.logger = MagicMock()
    context.data_dir = tmp_path / "xiaoqing_chat"
    event = {"message_type": "group", "group_id": 1, "user_id": 2}

    with patch(
        "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
        new=AsyncMock(
            side_effect=[
                ReplyRejected("需要重新规划", True),
                [{"type": "text", "data": {"text": "重规划后回复"}}],
            ]
        ),
    ) as mock_flow:
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

    with (
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

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
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
async def test_check_reply_rejects_when_llm_checker_credentials_missing():
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
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert result.suitable is True
    assert result.need_replan is False
    assert "checker" in result.reason.lower() or "unavailable" in result.reason.lower()


@pytest.mark.asyncio
async def test_check_reply_rejects_when_llm_checker_call_fails(monkeypatch: pytest.MonkeyPatch):
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
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert result.suitable is True
    assert result.need_replan is False
    assert "failed" in result.reason.lower() or "checker" in result.reason.lower()


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
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert result.suitable is False
    assert "完全相同" in result.reason or "高度相似" in result.reason


@pytest.mark.asyncio
async def test_check_reply_rejects_clarifying_placeholder_media_question():
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    history = [
        StoredMessage(
            role="user",
            name="Alice",
            parts=(
                {
                    "kind": "image",
                    "marker": "[图片：一张图片]",
                    "description": "一张图片",
                },
            ),
            ts=time.time(),
        )
    ]

    result = await check_reply(
        http_session=None,
        secrets={},
        bot_name="小青",
        reply="啥图啊",
        heuristic_reply="啥图啊",
        goal="聊天",
        policy_text="",
        history=history,
        chat_history_text="Alice: [图片：一张图片]",
        enable_llm_checker=False,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert result.suitable is False
    assert "占位信息" in result.reason


@pytest.mark.asyncio
async def test_check_reply_llm_prompt_mentions_media_markers(monkeypatch: pytest.MonkeyPatch):
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    captured: dict[str, str] = {}

    async def fake_chat(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return {"ok": True}, "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.llm_client.extract_response_content",
        lambda _resp: '{"suitable": true, "reason": "ok", "need_replan": false}',
    )

    result = await check_reply(
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        reply="咋了咋了[表情包：难过]",
        heuristic_reply="咋了咋了",
        goal="聊天",
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
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert result.suitable is True
    assert "待检查的最终回复" in captured["prompt"]
    assert "[表情包：...]" in captured["prompt"]
    assert "最终消息会附带相应媒体" in captured["prompt"]
    assert "没有为回复增加新的交流功能" in captured["prompt"]
