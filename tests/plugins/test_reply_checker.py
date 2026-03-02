"""Tests for reply_checker heuristic functions."""
import sys
import time
from unittest.mock import MagicMock

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
            history=history,
            bot_name="小青",
        )
        assert result is not None
        assert result.suitable is False

    def test_different_question_allowed(self):
        history = [
            _msg("assistant", "松松是谁啊", name="小青"),
        ]
        result = _check_repeated_question(
            reply="今天天气咋样",
            history=history,
            bot_name="小青",
        )
        assert result is None

    def test_non_question_reply_skipped(self):
        history = [
            _msg("assistant", "石景山路有啥特别的", name="小青"),
        ]
        result = _check_repeated_question(
            reply="哦这样啊",
            history=history,
            bot_name="小青",
        )
        assert result is None

    def test_no_history_allowed(self):
        result = _check_repeated_question(
            reply="松松是谁啊",
            history=[],
            bot_name="小青",
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
