import pytest

from plugins.xiaoqing_chat.memory.knowledge_extract import maybe_extract_person_facts
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB, person_fact_doc_id
from plugins.xiaoqing_chat.memory.memory_retrieval import (
    _execute_memory_tool,
    _tool_query_db,
    _tool_query_global_db,
)


def test_memory_query_requires_scope_and_never_crosses_chats(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id="a", text="same keyword from chat A", meta={"type": "person_info", "chat_id": "gA"}
    )
    db.upsert_text(
        doc_id="b", text="same keyword from chat B", meta={"type": "person_info", "chat_id": "gB"}
    )

    a_results = db.query("same keyword", chat_id="gA", top_k=10, min_score=0.0)
    b_results = db.query("same keyword", chat_id="gB", top_k=10, min_score=0.0)

    assert [item.doc_id for item in a_results] == ["a"]
    assert [item.doc_id for item in b_results] == ["b"]
    with pytest.raises(TypeError):
        db.query("same keyword")  # type: ignore[call-arg]


def test_global_memory_requires_explicit_approved_knowledge_type(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id="private", text="private group phrase", meta={"type": "person_info", "chat_id": "gA"}
    )
    db.upsert_text(
        doc_id="public", text="reviewed guide", meta={"type": "knowledge", "global_approved": True}
    )

    assert [
        item.doc_id for item in db.query_global("guide", type_filter="knowledge", min_score=0.0)
    ] == ["public"]
    with pytest.raises(ValueError):
        db.query_global("private", type_filter="person_info", min_score=0.0)


@pytest.mark.parametrize("memory_type", ["topic_summary", "person_info"])
def test_react_scoped_db_tools_pass_chat_id_and_never_cross_chats(tmp_path, memory_type):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id = f"a-{memory_type}",
        text   = "shared scoped phrase",
        meta   = {"type": memory_type, "chat_id": "chat-a", "subject_id": 7},
    )
    db.upsert_text(
        doc_id = f"b-{memory_type}",
        text   = "shared scoped phrase",
        meta   = {"type": memory_type, "chat_id": "chat-b", "subject_id": 7},
    )

    result = _tool_query_db(
        db,
        {"query": "shared scoped phrase", "subject_id": 7},
        type_filter = memory_type,
        chat_id     = "chat-a",
    )

    assert [item["doc_id"] for item in result["items"]] == [f"a-{memory_type}"]


@pytest.mark.parametrize("memory_type", ["knowledge", "word_def"])
def test_react_global_db_tools_use_only_approved_global_types(tmp_path, memory_type):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id = f"approved-{memory_type}",
        text   = "shared global phrase",
        meta   = {"type": memory_type, "global_approved": True},
    )
    db.upsert_text(
        doc_id = f"private-{memory_type}",
        text   = "shared global phrase",
        meta   = {"type": memory_type, "chat_id": "chat-a"},
    )

    result = _tool_query_global_db(
        db,
        {"query": "shared global phrase"},
        type_filter=memory_type,
    )

    assert [item["doc_id"] for item in result["items"]] == [f"approved-{memory_type}"]


def test_react_db_tool_failures_become_stable_observations(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)

    invalid = _execute_memory_tool(
        lambda args: _tool_query_db(
            db,
            args,
            type_filter = "person_info",
            chat_id     = "",
        ),
        {"query": "anything"},
    )

    assert invalid == {"error": "invalid_memory_tool_request"}


@pytest.mark.asyncio
async def test_identical_person_fact_has_independent_chat_scoped_ids_and_deletion(
    tmp_path,
    monkeypatch,
):
    async def fake_chat_completions(**_kwargs):
        return (
            '{"facts":[{"subject_id":42,"subject_name":"Alice",'
            '"fact":"喜欢喝茶","evidence":"Alice 说她喜欢喝茶"}]}'
        )

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.knowledge_extract.chat_completions",
        fake_chat_completions,
    )
    history = [
        StoredMessage(
            role    = "user",
            name    = "Alice",
            content = f"消息 {index}",
            ts      = float(index),
            user_id = 42,
        )
        for index in range(20)
    ]
    db = MemoryDB()
    db.bind(tmp_path)
    common = {
        "data_dir": tmp_path,
        "secrets": {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        "memory_db": db,
        "history": history,
        "temperature": 0.5,
        "top_p": 0.9,
        "max_tokens": 256,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    await maybe_extract_person_facts(chat_id="chat-a", **common)
    await maybe_extract_person_facts(chat_id="chat-b", **common)

    first_id = person_fact_doc_id(
        chat_id      = "chat-a",
        subject_id   = 42,
        subject_name = "Alice",
        fact         = "喜欢喝茶",
    )
    second_id = person_fact_doc_id(
        chat_id      = "chat-b",
        subject_id   = 42,
        subject_name = "Alice",
        fact         = "喜欢喝茶",
    )
    assert first_id != second_id
    assert db.get(first_id).meta["chat_id"] == "chat-a"
    assert db.get(second_id).meta["chat_id"] == "chat-b"
    assert [
        item.doc_id
        for item in db.query(
            "喜欢喝茶",
            chat_id     = "chat-a",
            type_filter = "person_info",
            min_score   = 0.0,
        )
    ] == [first_id]
    assert db.delete(first_id)
    assert db.get(first_id) is None
    assert db.get(second_id) is not None


def test_legacy_person_fact_migration_rekeys_valid_scope_and_quarantines_invalid(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id = "person:42:1234",
        text   = "Alice<42>：喜欢喝茶\n证据：Alice 说她喜欢喝茶",
        meta   = {
            "type": "person_info",
            "chat_id": "chat-a",
            "subject_id": 42,
            "subject_name": "Alice",
        },
    )
    db.upsert_text(
        doc_id = "person:43:5678",
        text   = "Bob<43>：喜欢咖啡\n证据：Bob 说他喜欢咖啡",
        meta   = {"type": "person_info", "subject_id": 43, "subject_name": "Bob"},
    )
    db.save()

    migrated = MemoryDB()
    migrated.bind(tmp_path)
    new_id = person_fact_doc_id(
        chat_id      = "chat-a",
        subject_id   = 42,
        subject_name = "Alice",
        fact         = "喜欢喝茶",
    )
    assert migrated.get("person:42:1234") is None
    assert migrated.get(new_id).meta["schema_version"] == 2
    quarantined = migrated.get("person:43:5678")
    assert quarantined is not None
    assert quarantined.meta["type"] == "quarantined_person_info"
    visible_ids = {
        item.doc_id
        for item in migrated.query(
            "喜欢咖啡",
            chat_id     = "chat-a",
            type_filter = "person_info",
            min_score   = 0.0,
        )
    }
    assert visible_ids == {new_id}
    assert "person:43:5678" not in visible_ids

    migrated.save()
    rebound = MemoryDB()
    rebound.bind(tmp_path)
    assert rebound.get(new_id) is not None
    assert rebound.get("person:43:5678").meta["type"] == "quarantined_person_info"
