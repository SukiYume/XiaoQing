import pytest

from plugins.xiaoqing_chat.memory.memory_db import MemoryDB


def test_memory_query_requires_scope_and_never_crosses_chats(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="a", text="same keyword from chat A", meta={"type": "person_info", "chat_id": "gA"})
    db.upsert_text(doc_id="b", text="same keyword from chat B", meta={"type": "person_info", "chat_id": "gB"})

    a_results = db.query("same keyword", chat_id="gA", top_k=10, min_score=0.0)
    b_results = db.query("same keyword", chat_id="gB", top_k=10, min_score=0.0)

    assert [item.doc_id for item in a_results] == ["a"]
    assert [item.doc_id for item in b_results] == ["b"]
    with pytest.raises(TypeError):
        db.query("same keyword")  # type: ignore[call-arg]


def test_global_memory_requires_explicit_approved_knowledge_type(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="private", text="private group phrase", meta={"type": "person_info", "chat_id": "gA"})
    db.upsert_text(doc_id="public", text="reviewed guide", meta={"type": "knowledge", "global_approved": True})

    assert [item.doc_id for item in db.query_global("guide", type_filter="knowledge", min_score=0.0)] == ["public"]
    with pytest.raises(ValueError):
        db.query_global("private", type_filter="person_info", min_score=0.0)
