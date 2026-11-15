from plugins.xiaoqing_chat.memory.memory_db import MemoryDB


def test_knowledge_query_only_returns_approved_knowledge(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id="private", text="private group guide", meta={"type": "person_info", "chat_id": "gA"}
    )
    db.upsert_text(doc_id="draft", text="draft guide", meta={"type": "knowledge"})
    db.upsert_text(
        doc_id="public", text="reviewed guide", meta={"type": "knowledge", "global_approved": True}
    )

    assert [item.doc_id for item in db.query_knowledge("guide", min_score=0.0)] == ["public"]
