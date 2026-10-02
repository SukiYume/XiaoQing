from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.adnmb import adapi
from plugins.bot_core import main as bot_core
from plugins.xiaoqing_chat import context_builder
from plugins.xiaoqing_chat import main as xiaoqing_chat
from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.expression import bw_jargon_miner
from plugins.xiaoqing_chat.llm.llm_client import LLMError
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.planning import pfc_action_planner

CANARY          = "CR219_INTERNAL_LOG_SECRET"
SENSITIVE_ERROR = (
    f"Authorization: Bearer {CANARY} "
    f"https://user:password@example.test/api?token={CANARY} "
    rf"C:\Users\victim\{CANARY}.txt"
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
    client = adapi.AdnmbClient(session=object(), cache_dir=tmp_path)

    with caplog.at_level(logging.WARNING):
        result = await client.download_image("private/image.png")

    logged = _log_text(caplog)
    assert result is None
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged
    assert "C:\\Users\\victim" not in logged


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
        assert "C:\\Users\\victim" not in logged
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
    assert "C:\\Users\\victim" not in logged


@pytest.mark.asyncio
async def test_xiaoqing_memory_failure_uses_correlated_public_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        context_builder,
        "_resolve_llm_config",
        Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )
    context = SimpleNamespace(
        http_session = object(),
        logger       = logging.getLogger("test.cr219.xiaoqing.memory"),
        request_id   = "req-cr219-memory",
        secrets      = {"token": CANARY},
    )
    runtime = SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=False)))

    with caplog.at_level(logging.ERROR):
        result = await context_builder._build_memory_block(
            context          = context,
            runtime          = runtime,
            state            = object(),
            secrets          = {"token": CANARY},
            data_dir         = tmp_path,
            chat_id          = "private-chat",
            history          = [],
            current_text     = "hello",
            planner_question = "",
            bot_name         = "小青",
        )

    logged = _log_text(caplog)
    assert result == ""
    assert "req-cr219-memory" in logged
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_xiaoqing_shutdown_failures_use_correlated_public_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = SimpleNamespace(
        stop_accepting_background_tasks=Mock(),
        background_tasks=Mock(return_value=set()),
        memory_store=SimpleNamespace(persist_all=Mock()),
        pfc_state_store=SimpleNamespace(flush=Mock()),
        action_history=SimpleNamespace(
            flush=Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
        ),
        media_store=SimpleNamespace(flush=Mock()),
        memory_db=SimpleNamespace(is_dirty=Mock(return_value=False)),
    )
    monkeypatch.setattr(xiaoqing_chat, "_state", Mock(return_value=state))
    context = SimpleNamespace(
        logger     = logging.getLogger("test.cr219.xiaoqing.shutdown"),
        request_id = "req-cr219-shutdown",
        secrets    = {"token": CANARY},
    )

    with caplog.at_level(logging.ERROR):
        await xiaoqing_chat.shutdown(context)

    logged = _log_text(caplog)
    assert "req-cr219-shutdown" in logged
    assert "RuntimeError" in logged
    assert CANARY not in logged
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_xiaoqing_internal_llm_fallback_logs_only_type(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_fallback_paths",
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
            temperature=0.2,
            top_p=0.8,
            max_tokens=200,
            timeout_seconds=1,
            max_retry=0,
            retry_interval_seconds=0,
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
        "chat_completions_raw_with_fallback_paths",
        AsyncMock(side_effect=LLMError(SENSITIVE_ERROR)),
    )
    history = [StoredMessage(role="user", name="user", ts=1.0, content="hello")]

    with caplog.at_level(logging.WARNING):
        result = await bw_jargon_miner.mine_jargon(
            secrets={"api_base": "https://example.test", "api_key": "key", "model": "m"},
            store=Mock(),
            chat_id="chat-1",
            messages=history,
            bot_name="小青",
            temperature=0.2,
            top_p=0.8,
            max_tokens=500,
            timeout_seconds=1,
            max_retry=0,
            retry_interval_seconds=0,
        )

    logged = _log_text(caplog)
    assert result == 0
    assert "AIError" in logged
    assert CANARY not in logged
    assert "user:password" not in logged
