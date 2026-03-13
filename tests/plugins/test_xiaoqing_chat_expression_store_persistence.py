import json

from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord, ExpressionStore
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
from plugins.xiaoqing_chat.expression.bw_message_recorder import MessageRecorder
from plugins.xiaoqing_chat.expression.bw_reflect_tracker import ReflectTrackerStore


def test_expression_store_load_save_roundtrip(tmp_path):
    store = ExpressionStore()
    store.bind(tmp_path)
    store.save(
        [
            ExpressionRecord(
                expression_id="exp-1",
                chat_id="chat-1",
                situation="s1",
                style="st1",
                content_list=["hello", "world"],
                count=3,
                last_active_time=123.0,
                checked=True,
                rejected=False,
                modified_by="user",
            )
        ]
    )

    reloaded = ExpressionStore()
    reloaded.bind(tmp_path)
    items = reloaded.load()

    assert len(items) == 1
    assert items[0].expression_id == "exp-1"
    assert items[0].chat_id == "chat-1"
    assert items[0].content_list == ["hello", "world"]
    assert items[0].checked is True
    assert items[0].modified_by == "user"


def test_jargon_store_load_save_roundtrip(tmp_path):
    store = JargonStore()
    store.bind(tmp_path)
    store.save(
        [
            JargonRecord(
                content="梗",
                meaning="meaning",
                raw_content=["a", "b"],
                chat_id_counts=[["chat-1", 2]],
                is_global=True,
                count=2,
                is_jargon=True,
                is_complete=True,
                last_inference_count=1,
                updated_at=456.0,
            )
        ]
    )

    reloaded = JargonStore()
    reloaded.bind(tmp_path)
    items = reloaded.load()

    assert set(items.keys()) == {"梗"}
    rec = items["梗"]
    assert rec.meaning == "meaning"
    assert rec.raw_content == ["a", "b"]
    assert rec.chat_id_counts == [["chat-1", 2]]
    assert rec.is_global is True
    assert rec.is_complete is True


def test_reflect_tracker_store_persistence(tmp_path):
    store = ReflectTrackerStore()
    store.bind(tmp_path)
    store.set_tracker("chat-1", "exp-1")

    reloaded = ReflectTrackerStore()
    reloaded.bind(tmp_path)
    loaded = reloaded.load()

    assert "chat-1" in loaded
    state = loaded["chat-1"]
    assert state.operator_chat_id == "chat-1"
    assert state.expression_id == "exp-1"
    assert state.created_time > 0


def test_message_recorder_persistence(tmp_path):
    recorder = MessageRecorder()
    recorder.bind(tmp_path)
    recorder.set_last_time("chat-1", 789.5)

    reloaded = MessageRecorder()
    reloaded.bind(tmp_path)

    assert reloaded.get_last_time("chat-1") == 789.5


def test_message_recorder_malformed_file_fallback_then_persist(tmp_path):
    learner_dir = tmp_path / "bw_learner"
    learner_dir.mkdir(parents=True, exist_ok=True)
    (learner_dir / "message_recorder.json").write_text("{not-json", encoding="utf-8")

    recorder = MessageRecorder()
    recorder.bind(tmp_path)

    assert recorder.get_last_time("chat-2") == 0.0

    recorder.set_last_time("chat-2", 42.0)
    saved = json.loads((learner_dir / "message_recorder.json").read_text(encoding="utf-8"))
    assert saved == {"last_extraction_time": {"chat-2": 42.0}}
