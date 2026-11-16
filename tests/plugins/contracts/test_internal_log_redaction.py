from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.adnmb import adapi
from plugins.bot_core import main as bot_core
from plugins.xiaoqing_chat import main as xiaoqing_chat
from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.expression import bw_jargon_miner
from plugins.xiaoqing_chat.llm.gateway import LLMError
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory import client as memory_client
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.planning import pfc_action_planner
from plugins.xiaoqing_chat.planning.action_history import ActionRecord
from plugins.xiaoqing_chat.store_binding import bound_state

CANARY          = "INTERNAL_LOG_SECRET"
SENSITIVE_ERROR = (
    f"Authorization: Bearer {CANARY} "
    f"https://user:password@example.test/api?token={CANARY} "
    rf"C:\Users\user\{CANARY}.txt"
)


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_adnmb_internal_download_error_logs_only_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        adapi,
        "fetch_public_bytes",
        AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )
    client = adapi.AdnmbClient(session=object(), cache_dir=tmp_path, uuid="uuid")

    with caplog.at_level(logging.WARNING):
        result = await client.download_image("private/image.png")

    logged = _log_text(caplog)
    assert result is None
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged
    assert "C:\\Users\\user" not in logged


def test_arxiv_internal_request_error_logs_only_type(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module_name     = "plugins.arxiv_filter.arxiv_today"
    fake_feedparser = ModuleType("feedparser")
    monkeypatch.setitem(sys.modules, "feedparser", fake_feedparser)
    sys.modules.pop(module_name, None)
    try:
        arxiv_today = importlib.import_module(module_name)
        monkeypatch.setattr(
            arxiv_today,
            "requests_request_bounded",
            Mock(side_effect=arxiv_today.requests.RequestException(SENSITIVE_ERROR)),
        )

        with caplog.at_level(logging.ERROR):
            result = arxiv_today._fetch_arxiv_page(
                "https://arxiv.org/list/astro-ph/new",
                {"arxiv": {"timeout": 1, "use_ssl_verify": True}},
            )

        logged = _log_text(caplog)
        assert result is None
        assert "RequestException" in logged
        assert CANARY not in logged
        assert "user:password" not in logged
        assert "C:\\Users\\user" not in logged
    finally:
        sys.modules.pop(module_name, None)


def test_bot_core_mask_failure_logs_only_type(caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingString(str):
        def __len__(self) -> int:
            raise RuntimeError(SENSITIVE_ERROR)

    with caplog.at_level(logging.ERROR):
        result = bot_core.mask_secret(ExplodingString("secret"))

    logged = _log_text(caplog)
    assert result == "[error]"
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged
    assert "C:\\Users\\user" not in logged


@pytest.mark.asyncio
async def test_xiaoqing_memory_failure_logs_only_the_error_type(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    class ExplodingMemory:
        async def recall(self, _payload):
            raise RuntimeError(SENSITIVE_ERROR)

    context = SimpleNamespace(
        data_dir=tmp_path, capabilities=SimpleNamespace(memory=ExplodingMemory())
    )

    with caplog.at_level(logging.INFO):
        result = await memory_client.recall(context, purpose="reply", chat_id="u1", text="hello")

    logged = _log_text(caplog)
    assert not result.available and result.blocks == {}
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_xiaoqing_shutdown_failures_use_correlated_public_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    context = SimpleNamespace(
        data_dir   = tmp_path,
        logger     = logging.getLogger("test.xiaoqing.shutdown"),
        request_id = "req-shutdown",
        secrets    = {"token": CANARY},
    )
    state = bound_state(context)
    state.action_history.append(
        "chat-1",
        ActionRecord(
            ts           = 1.0,
            local_target = "chat-1",
            action       = "reply",
            reasoning    = "",
            detail       = {},
            executed     = True,
        ),
    )
    real_replace = os.replace

    def replace_that_fails_for_action_history(src, dst, *args, **kwargs) -> None:
        # 在 OS 层让行动历史落盘失败，错误文字里带着泄漏探针。
        if Path(dst).parent == tmp_path / "action_history":
            raise OSError(SENSITIVE_ERROR)
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_that_fails_for_action_history)

    with caplog.at_level(logging.ERROR):
        await xiaoqing_chat.shutdown(context)

    logged = _log_text(caplog)
    assert "req-shutdown" in logged
    assert "OSError" in logged
    assert CANARY not in logged
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_xiaoqing_internal_llm_fallback_logs_only_type(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_profile",
        AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.INFO):
        result = await pfc_action_planner.plan_next_action(
            secrets={"api_base": "https://example.test", "api_key": "key", "model": "m"},
            bot_name="小青",
            is_private=True,
            personality=PersonalityConfig(),
            history=[],
            goal_list=[],
            knowledge_list=[],
            action_history_summary="",
            last_action_context="",
            timeout_context="",
            last_successful_reply_action=None,
            llm=LLMCallConfig(
                temperature            = 0.2,
                top_p                  = 0.8,
                max_tokens             = 200,
                timeout_seconds        = 1,
                max_retry              = 0,
                retry_interval_seconds = 0,
            ),
        )

    logged = _log_text(caplog)
    assert result.reason == "planner_failed"
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged


@pytest.mark.asyncio
async def test_xiaoqing_jargon_fallback_logs_only_type(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        bw_jargon_miner,
        "chat_completions_raw_with_profile",
        AsyncMock(side_effect=LLMError(SENSITIVE_ERROR)),
    )
    history = [StoredMessage(role="user", name="user", ts=1.0, content="hello")]

    with caplog.at_level(logging.WARNING):
        result = await bw_jargon_miner.mine_jargon(
            secrets  = {"api_base": "https://example.test", "api_key": "key", "model": "m"},
            store    = Mock(),
            chat_id  = "chat-1",
            messages = history,
            bot_name = "小青",
            llm      = LLMCallConfig(
                temperature            = 0.2,
                top_p                  = 0.8,
                max_tokens             = 500,
                timeout_seconds        = 1,
                max_retry              = 0,
                retry_interval_seconds = 0,
            ),
        )

    logged = _log_text(caplog)
    assert result == 0
    assert "AIError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged
