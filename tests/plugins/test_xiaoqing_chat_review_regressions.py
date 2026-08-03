from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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

    assert is_brain_chat_active(runtime, is_private=True) is True


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

    rendered = str(result)
    assert "XQ-PLUGIN-UNEXPECTED" in rendered
    assert "internal" not in rendered
    assert "/tmp/secrets" not in rendered


@pytest.mark.asyncio
async def test_build_memory_block_fallback_keeps_global_memory_hits() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB, RetrievedItem
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    async def fake_react_retrieve(**kwargs) -> str:
        return ""

    def fake_query(question, *, chat_id, top_k, min_score, type_filter=None, meta_filter=None):
        assert chat_id == "group-1"
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
        )

    assert "全局知识命中" in block


@pytest.mark.asyncio
async def test_build_memory_block_uses_current_text_when_question_generation_fails() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB, RetrievedItem
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    queried: list[str] = []

    async def fake_react_retrieve(**kwargs) -> str:
        return ""

    def fake_query(question, *, chat_id, top_k, min_score, type_filter=None, meta_filter=None):
        assert chat_id == "g1"
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
        )

    assert queried == ["王府井以前有二次元店吗"]
    assert "王府井二次元店讨论摘要" in block


@pytest.mark.asyncio
async def test_memory_block_is_bounded_so_recent_dialogue_stays_primary() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB, RetrievedItem
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    memory_db = MemoryDB()
    memory_db.query = MagicMock(
        return_value=[
            RetrievedItem(
                doc_id="topic:long",
                text="旧摘要" * 1000,
                score=1.0,
                meta={"type": "topic_summary", "chat_id": "g1"},
            )
        ]
    )
    cfg = MemoryConfig(
        planner_question=False,
        enable_thinking_back_cache=False,
        top_k=3,
        max_block_chars=120,
    )

    block = await build_memory_block(
        data_dir=Path("."),
        chat_id="g1",
        secrets={"_ai": None},
        cfg=cfg,
        bot_name="小青",
        history=[],
        current_text="现在聊什么",
        planner_question="",
        memory_db=memory_db,
        temperature=0.7,
        top_p=0.9,
        max_tokens=256,
        timeout_seconds=3.0,
    )

    assert block.startswith("你回忆起了以下信息：")
    assert len(block) <= 140


def test_topic_summarizer_prompt_requires_attribution_and_uncertainty() -> None:
    from plugins.xiaoqing_chat.llm.summarizer import build_topic_messages

    messages = build_topic_messages(bot_name="小青", history=[])
    system_prompt = messages[0].content

    assert "保留信息来源、说话人和不确定性" in system_prompt
    assert "不能升级成角色的真实经历" in system_prompt
    assert "不补写动机、关系或背景" in system_prompt
    assert all(marker not in system_prompt for marker in ("例如", "比如", "示例："))


@pytest.mark.asyncio
async def test_build_memory_block_does_not_forward_provider_overrides() -> None:
    from plugins.xiaoqing_chat.config.config import MemoryConfig
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    captured: dict[str, object] = {}

    async def fake_react_retrieve(**kwargs) -> str:
        captured["has_extra_payload"] = "extra_payload" in kwargs
        return "记得她之前说过喜欢吃辣"

    def fake_query(question, *, chat_id, top_k, min_score, type_filter=None, meta_filter=None):
        assert chat_id == "g1"
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
        )

    assert "喜欢吃辣" in block
    assert captured["has_extra_payload"] is False


def test_llm_call_config_contains_only_runtime_controls() -> None:
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig

    config = LLMCallConfig(timeout_seconds=3.0, max_retry=0, retry_interval_seconds=0.2)

    assert config.to_dict() == {
        "timeout_seconds": 3.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.2,
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
async def test_xiaoqing_gateway_delegates_retry_policy_to_core() -> None:
    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import complete_raw

    service = SimpleNamespace(
        complete=AsyncMock(
            return_value=AICompletionResult(
                response={"choices": [{"message": {"content": "ok"}}]},
                profile="primary",
                provider="provider",
                model="model",
                finish_reason="stop",
                attempts=1,
            )
        )
    )
    await complete_raw(
        secrets={"_ai": service, "_route": "chat"},
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=3.0,
        max_retry=2,
        retry_interval_seconds=0.5,
    )

    kwargs = service.complete.await_args.kwargs
    assert kwargs["max_retry"] == 2
    assert kwargs["retry_interval_seconds"] == 0.5
    assert kwargs["timeout_seconds"] == 3.0


@pytest.mark.asyncio
async def test_gateway_forwards_extra_payload_without_streaming() -> None:
    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import complete_raw

    service = SimpleNamespace(
        complete=AsyncMock(
            return_value=AICompletionResult(
                response={"choices": [{"message": {"content": "ok"}}]},
                profile="primary",
                provider="provider",
                model="model",
                finish_reason="stop",
                attempts=1,
            )
        )
    )
    messages = [{"role": "user", "content": "hi"}]

    await complete_raw(
        secrets={"_ai": service, "_route": "chat"},
        messages=messages,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.5,
        extra_payload={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    )

    kwargs = service.complete.await_args.kwargs
    assert kwargs["extra_payload"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert service.complete.await_args.args[1] == messages


@pytest.mark.asyncio
async def test_xiaoqing_uses_safe_core_model_metadata(tmp_path) -> None:
    from core.ai import AIModelInfo
    from core.interfaces import PluginCapabilities
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config
    from plugins.xiaoqing_chat.helper_utils import _get_ai_route_context
    from plugins.xiaoqing_chat.runtime_state import get_state

    state = get_state()
    old_active = state.global_active_provider
    state.set_global_provider(None)
    try:
        config = {
            "plugins": {
                "xiaoqing_chat": {
                    "timeout_seconds": 23,
                    "ai": {
                        "default_model_alias": "deepseek",
                        "model_aliases": {"deepseek": "deepseek-flash"},
                    },
                }
            }
        }
        service = SimpleNamespace(
            list_models=lambda route, **kwargs: (
                AIModelInfo(
                    "deepseek-flash",
                    "deepseek",
                    "deepseek-v4-flash",
                    ("text",),
                ),
            )
        )
        from tests.helpers.settings_snapshot import with_settings_reader

        context = with_settings_reader(
            SimpleNamespace(
                config=config,
                secrets={"plugins": {"xiaoqing_chat": {"api_key": "<LEGACY_API_KEY>"}}},
                capabilities=PluginCapabilities(ai=service),
            )
        )

        secrets = _get_ai_route_context(context)
        loaded = load_xiaoqing_chat_config(
            context_config=config,
            plugin_dir=tmp_path,
        )
    finally:
        state.set_global_provider(old_active)

    assert secrets["model"] == "deepseek-v4-flash"
    assert secrets["_profile"] == "deepseek-flash"
    assert secrets["_pinned_model"] is None
    assert "api_key" not in secrets and "api_base" not in secrets
    assert loaded.timeout_seconds == 23
