from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.xiaoqing_chat.config.config import MemoryConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.memory_retrieval import (
    _execute_memory_tool,
    _extract_tool_calls,
    _tool_query_db,
    _tool_query_global_db,
    react_retrieve,
)


def _tool_response(name: str, arguments: dict, *, call_id: str = "call-1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _react_kwargs(*, history: list[StoredMessage] | None = None) -> dict:
    return {
        "secrets": {},
        "cfg": MemoryConfig(max_agent_iterations=5, agent_timeout_seconds=10.0),
        "history": history or [],
        "chat_id": "g1",
        "question": "她之前说喜欢什么？",
        "memory_db": MagicMock(),
        "temperature": 0.5,
        "top_p": 0.9,
        "max_tokens": 256,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }


def test_extract_tool_calls_normalizes_missing_and_duplicate_ids() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "same",
                            "function": {"name": "query_words", "arguments": '{"query":"x"}'},
                        },
                        {
                            "id": "same",
                            "function": {"name": "query_knowledge", "arguments": "{}"},
                        },
                        {
                            "function": {"name": "not_enough_info", "arguments": "{}"},
                        },
                    ]
                }
            }
        ]
    }

    calls = _extract_tool_calls(response)

    assert [call.name for call in calls] == [
        "query_words",
        "query_knowledge",
        "not_enough_info",
    ]
    assert len({call.call_id for call in calls}) == 3


def test_scoped_tool_rejects_malformed_subject_instead_of_broadening_query() -> None:
    memory_db = MagicMock()

    result = _execute_memory_tool(
        lambda args: _tool_query_db(
            memory_db,
            args,
            type_filter = "person_info",
            chat_id     = "g1",
        ),
        {"query": "喜欢什么", "subject_id": "not-an-id"},
    )

    assert result == {"error": "invalid_memory_tool_request"}
    memory_db.query.assert_not_called()


def test_global_memory_tool_redacts_legacy_absolute_path_metadata() -> None:
    from plugins.xiaoqing_chat.memory.memory_db import RetrievedItem

    windows_path                        = r"C:\Users\operator\private\knowledge.txt"
    posix_path                          = "/home/operator/private/knowledge.txt"
    memory_db                           = MagicMock()
    memory_db.query_global.return_value = [
        RetrievedItem(
            doc_id = "kb:legacy:0",
            text   = "approved knowledge",
            score  = 0.9,
            meta   = {
                "type": "knowledge",
                "source": windows_path,
                "source_path": posix_path,
            },
        )
    ]

    result = _tool_query_global_db(
        memory_db,
        {"query": "knowledge", "top_k": 1},
        type_filter="knowledge",
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert windows_path not in serialized
    assert posix_path not in serialized
    assert "source_path" not in result["items"][0]["meta"]
    assert result["items"][0]["meta"]["source"].startswith("local-ref:")


@pytest.mark.asyncio
async def test_react_requires_retrieval_evidence_before_accepting_answer(monkeypatch) -> None:
    responses = [
        _tool_response("found_answer", {"answer": "凭空答案"}),
        _tool_response("query_chat_history", {"query": "喜欢辣"}),
        _tool_response("found_answer", {"answer": "她说过喜欢吃辣"}),
    ]
    observed_messages: list[list[dict]] = []

    async def complete(**kwargs):
        observed_messages.append(deepcopy(kwargs["messages"]))
        return responses.pop(0), "/v1/chat/completions"

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.chat_completions_raw_with_fallback_paths",
        complete,
    )
    history = [StoredMessage(role="user", name="甲", content="我喜欢辣", ts=1.0, user_id=1)]

    answer = await react_retrieve(**_react_kwargs(history=history))

    assert answer == "她说过喜欢吃辣"
    assert len(observed_messages) == 3
    assert "memory_evidence_required" in observed_messages[1][-1]["content"]


@pytest.mark.asyncio
async def test_react_not_enough_info_terminates_without_extra_api_calls(monkeypatch) -> None:
    complete = AsyncMock(
        return_value=(
            _tool_response("not_enough_info", {"reason": "没有记录"}),
            "/v1/chat/completions",
        )
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.chat_completions_raw_with_fallback_paths",
        complete,
    )

    answer = await react_retrieve(**_react_kwargs())

    assert answer == ""
    assert complete.await_count == 1


@pytest.mark.asyncio
async def test_react_rejects_bare_model_answer_without_tool_evidence(monkeypatch) -> None:
    complete = AsyncMock(
        return_value=(
            {"choices": [{"message": {"content": "模型自己猜的答案"}}]},
            "/v1/chat/completions",
        )
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.chat_completions_raw_with_fallback_paths",
        complete,
    )

    answer = await react_retrieve(**_react_kwargs())

    assert answer == ""
    assert complete.await_count == 1


@pytest.mark.asyncio
async def test_memory_direct_miss_skips_agent_for_ordinary_message(
    monkeypatch,
    tmp_path,
) -> None:
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    memory_db = MemoryDB()
    memory_db.query = MagicMock(return_value=[])
    retrieve = AsyncMock(return_value="不应被调用")
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
        retrieve,
    )

    block = await build_memory_block(
        data_dir = tmp_path,
        chat_id  = "g1",
        secrets  = {"_ai": object()},
        cfg      = MemoryConfig(
            planner_question                        = False,
            enable_thinking_back_cache              = False,
            agent_on_direct_miss_requires_reference = True,
        ),
        bot_name         = "小青",
        history          = [],
        current_text     = "冻饺子怎么煮",
        planner_question = "",
        memory_db        = memory_db,
        temperature      = 0.7,
        top_p            = 0.9,
        max_tokens       = 256,
        timeout_seconds  = 3.0,
    )

    assert block == ""
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_direct_query_runs_outside_event_loop_thread(monkeypatch, tmp_path) -> None:
    import threading

    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    event_loop_thread           = threading.get_ident()
    observed_threads: list[int] = []
    memory_db                   = MagicMock()

    def query(*_args, **_kwargs):
        observed_threads.append(threading.get_ident())
        return []

    memory_db.query.side_effect = query
    retrieve = AsyncMock(return_value="")
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
        retrieve,
    )

    await build_memory_block(
        data_dir = tmp_path,
        chat_id  = "g1",
        secrets  = {"_ai": object()},
        cfg      = MemoryConfig(
            planner_question                        = False,
            enable_thinking_back_cache              = False,
            agent_on_direct_miss_requires_reference = True,
        ),
        bot_name         = "小青",
        history          = [],
        current_text     = "普通消息",
        planner_question = "",
        memory_db        = memory_db,
        temperature      = 0.7,
        top_p            = 0.9,
        max_tokens       = 256,
        timeout_seconds  = 3.0,
    )

    assert observed_threads
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


@pytest.mark.asyncio
async def test_memory_direct_miss_keeps_agent_for_explicit_recall(
    monkeypatch,
    tmp_path,
) -> None:
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
    from plugins.xiaoqing_chat.memory.memory_retrieval import build_memory_block

    memory_db = MemoryDB()
    memory_db.query = MagicMock(return_value=[])
    retrieve = AsyncMock(return_value="她之前说过喜欢清淡口味")
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory_retrieval.react_retrieve",
        retrieve,
    )

    block = await build_memory_block(
        data_dir = tmp_path,
        chat_id  = "g1",
        secrets  = {"_ai": object()},
        cfg      = MemoryConfig(
            planner_question                        = False,
            enable_thinking_back_cache              = False,
            agent_on_direct_miss_requires_reference = True,
        ),
        bot_name         = "小青",
        history          = [],
        current_text     = "她之前说过喜欢什么来着",
        planner_question = "",
        memory_db        = memory_db,
        temperature      = 0.7,
        top_p            = 0.9,
        max_tokens       = 256,
        timeout_seconds  = 3.0,
    )

    assert "喜欢清淡口味" in block
    retrieve.assert_awaited_once()
