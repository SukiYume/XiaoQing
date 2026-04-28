from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest


def test_is_brain_chat_active_keeps_private_brain_mode_when_forced() -> None:
    from plugins.xiaoqing_chat.brain_chat import is_brain_chat_active
    from plugins.xiaoqing_chat.config.config import BrainChatConfig, XiaoQingChatConfig
    from plugins.xiaoqing_chat.runtime_state import _ChatRuntime

    runtime = _ChatRuntime(
        cfg=XiaoQingChatConfig(
            brain_chat=BrainChatConfig(enable_private_brain_chat=True),
        ),
        compiled_ban_regex=[],
    )

    assert is_brain_chat_active(runtime, is_private=True, forced=True) is True


@pytest.mark.asyncio
async def test_handle_smalltalk_hides_internal_exception_details() -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    context = MagicMock()
    context.logger = MagicMock()

    event = {
        "message_type": "private",
        "user_id": 123,
        "message_id": 456,
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
            AsyncMock(side_effect=RuntimeError("internal /tmp/secrets/token.txt leak")),
        )
        result = await handle_smalltalk("你好", event, context)

    assert result == [{"type": "text", "data": {"text": "❌ 对话处理出错，请稍后再试"}}]


@pytest.mark.asyncio
async def test_build_memory_block_fallback_keeps_global_memory_hits() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_db import RetrievedItem
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    async def fake_react_retrieve(**kwargs) -> str:
        return ""

    def fake_query(question, *, top_k, min_score, type_filter=None, meta_filter=None):
        assert meta_filter is None
        return [
            RetrievedItem(
                doc_id="knowledge:1",
                text="全局知识命中",
                score=0.9,
                meta={"type": "knowledge"},
            )
        ]

    memory_db = MemoryDB()
    cfg = MemoryConfig(
        planner_question=False,
        enable_thinking_back_cache=False,
        top_k=2,
        min_score=0.1,
        max_agent_iterations=1,
        agent_timeout_seconds=1.0,
        thinking_back_window_seconds=0.0,
    )
    history = [StoredMessage(role="user", name="Tester", content="你好", ts=1.0)]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memory_db, "query", fake_query)
        mp.setattr(
            "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
            fake_react_retrieve,
        )
        block = await build_memory_block(
            data_dir=Path("."),
            chat_id="group-1",
            http_session=object(),
            secrets={},
            cfg=cfg,
            bot_name="小青",
            history=history,
            current_text="你好",
            planner_question="问点啥",
            memory_db=memory_db,
            temperature=0.7,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            max_retry=0,
            retry_interval_seconds=0.2,
            proxy="",
            endpoint_path="/v1/chat/completions",
        )

    assert "全局知识命中" in block


@pytest.mark.asyncio
async def test_build_memory_block_uses_current_text_when_question_generation_fails() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_db import RetrievedItem
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    queried: list[str] = []

    async def fake_react_retrieve(**kwargs) -> str:
        return ""

    def fake_query(question, *, top_k, min_score, type_filter=None, meta_filter=None):
        queried.append(question)
        return [
            RetrievedItem(
                doc_id="topic:1",
                text="王府井二次元店讨论摘要",
                score=0.9,
                meta={"type": "topic_summary", "chat_id": "g1"},
            )
        ]

    memory_db = MemoryDB()
    cfg = MemoryConfig(
        planner_question=True,
        enable_thinking_back_cache=False,
        top_k=2,
        min_score=0.1,
        max_agent_iterations=1,
        agent_timeout_seconds=1.0,
    )
    history = [StoredMessage(role="user", name="Tester", content="你好", ts=1.0)]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memory_db, "query", fake_query)
        mp.setattr(
            "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
            fake_react_retrieve,
        )
        block = await build_memory_block(
            data_dir=Path("."),
            chat_id="g1",
            http_session=object(),
            secrets={},
            cfg=cfg,
            bot_name="小青",
            history=history,
            current_text="王府井以前有二次元店吗",
            planner_question="",
            memory_db=memory_db,
            temperature=0.7,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            max_retry=0,
            retry_interval_seconds=0.2,
            proxy="",
            endpoint_path="/v1/chat/completions",
        )

    assert queried == ["王府井以前有二次元店吗"]
    assert "王府井二次元店讨论摘要" in block


@pytest.mark.asyncio
async def test_build_memory_block_uses_control_payload_for_react() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    captured: dict[str, object] = {}

    async def fake_react_retrieve(**kwargs) -> str:
        captured["extra_payload"] = kwargs.get("extra_payload")
        return "记得她之前说过喜欢吃辣"

    def fake_query(question, *, top_k, min_score, type_filter=None, meta_filter=None):
        return []

    memory_db = MemoryDB()
    cfg = MemoryConfig(
        planner_question=False,
        enable_thinking_back_cache=False,
        top_k=2,
        min_score=0.1,
        max_agent_iterations=1,
        agent_timeout_seconds=1.0,
    )
    history = [StoredMessage(role="user", name="Tester", content="你好", ts=1.0)]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memory_db, "query", fake_query)
        mp.setattr(
            "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
            fake_react_retrieve,
        )
        block = await build_memory_block(
            data_dir=Path("."),
            chat_id="g1",
            http_session=object(),
            secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
            cfg=cfg,
            bot_name="小青",
            history=history,
            current_text="她喜欢吃什么来着",
            planner_question="她喜欢吃什么来着",
            memory_db=memory_db,
            temperature=0.7,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            max_retry=0,
            retry_interval_seconds=0.2,
            proxy="",
            endpoint_path="/v1/chat/completions",
            extra_payload={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        )

    assert "喜欢吃辣" in block
    assert captured["extra_payload"] == {"thinking": {"type": "disabled"}}


def test_control_extra_payload_preserves_non_reasoning_fields_and_disables_thinking() -> None:
    from plugins.xiaoqing_chat.llm.control_payload import control_extra_payload

    payload = control_extra_payload(
        {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "seed": 123,
        },
        json_object=True,
    )

    assert payload == {
        "thinking": {"type": "disabled"},
        "seed": 123,
        "response_format": {"type": "json_object"},
    }


def test_memory_db_save_logs_write_failures(tmp_path) -> None:
    from plugins.xiaoqing_chat.memory import memory_db as memory_db_module
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    logger = MagicMock()

    def fail_write_vector_store_files(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memory_db_module, "_logger", logger, raising=False)
        mp.setattr(
            memory_db_module,
            "write_vector_store_files",
            fail_write_vector_store_files,
            raising=True,
        )
        db.save()

    assert db.is_dirty() is True
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_chat_completions_raw_uses_exponential_backoff() -> None:
    from plugins.xiaoqing_chat.llm.llm_client import LLMError, chat_completions_raw

    sleep_calls = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    class FakeResponse:
        status = 500

        async def text(self) -> str:
            return "server busy"

        async def json(self):
            return {}

    class FakePostContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_mock = MagicMock(spec=aiohttp.ClientSession)
    session_mock.post = MagicMock(return_value=FakePostContext())
    session = cast(aiohttp.ClientSession, session_mock)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("plugins.xiaoqing_chat.llm.llm_client.asyncio.sleep", fake_sleep)
        with pytest.raises(LLMError):
            await chat_completions_raw(
                session=session,
                api_base="https://example.com",
                api_key="k",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.7,
                top_p=0.9,
                max_tokens=128,
                timeout_seconds=3.0,
                max_retry=2,
                retry_interval_seconds=0.5,
                proxy="",
                endpoint_path="/v1/chat/completions",
            )

    assert sleep_calls == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_chat_completions_raw_merges_extra_payload_without_streaming() -> None:
    from plugins.xiaoqing_chat.llm.llm_client import chat_completions_raw

    class FakeResponse:
        status = 200

        async def text(self) -> str:
            return ""

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakePostContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_mock = MagicMock(spec=aiohttp.ClientSession)
    session_mock.post = MagicMock(return_value=FakePostContext())
    session = cast(aiohttp.ClientSession, session_mock)
    messages = [{"role": "user", "content": "hi"}]

    await chat_completions_raw(
        session=session,
        api_base="https://example.com",
        api_key="k",
        model="m",
        messages=messages,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.5,
        proxy="",
        endpoint_path="/v1/chat/completions",
        extra_payload={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "stream": True,
            "model": "override",
            "messages": [],
        },
    )

    payload = session_mock.post.call_args.kwargs["json"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["stream"] is False
    assert payload["model"] == "m"
    assert payload["messages"] == messages


def test_llm_provider_extras_flow_into_call_config() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.helper_utils import _get_llm_secrets, _resolve_llm_config
    from plugins.xiaoqing_chat.runtime_state import get_state

    state = get_state()
    old_active = state.active_provider
    state.active_provider = None
    try:
        context = SimpleNamespace(
            secrets={
                "plugins": {
                    "xiaoqing_chat": {
                        "default": "deepseek",
                        "providers": {
                            "deepseek": {
                                "api_base": "https://api.deepseek.com",
                                "api_key": "k",
                                "model": "deepseek-v4-flash",
                                "thinking": {"type": "enabled"},
                                "reasoning_effort": "high",
                            }
                        },
                    }
                }
            }
        )

        secrets = _get_llm_secrets(context)
        cfg = _resolve_llm_config(XiaoQingChatConfig(), secrets)
    finally:
        state.active_provider = old_active

    assert secrets["model"] == "deepseek-v4-flash"
    assert cfg.extra_payload == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert cfg.to_dict()["extra_payload"] == cfg.extra_payload
