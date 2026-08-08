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


def test_expression_store_keeps_record_with_malformed_scalar_fields(tmp_path):
    path = tmp_path / "bw_learner" / "expressions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "expression_id": "exp-bad-fields",
                    "chat_id": "chat-1",
                    "situation": "situation",
                    "style": "style",
                    "content_list": ["example"],
                    "count": "not-an-int",
                    "last_active_time": "nan",
                    "checked": "false",
                    "rejected": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    store = ExpressionStore()
    store.bind(tmp_path)

    record = store.load()[0]

    assert record.expression_id == "exp-bad-fields"
    assert record.count == 1
    assert record.last_active_time == 0.0
    assert record.checked is False
    assert record.rejected is False


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


def test_jargon_store_normalizes_malformed_record_fields(tmp_path):
    path = tmp_path / "bw_learner" / "jargon.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "content": "坏字段也保留条目",
                    "raw_content": "不能拆成字符",
                    "chat_id_counts": ["bad", ["chat-1", "2"], ["", 9]],
                    "is_global": "false",
                    "count": "not-an-int",
                    "is_jargon": "false",
                    "is_complete": 1,
                    "last_inference_count": -3,
                    "updated_at": "not-a-time",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = JargonStore()
    store.bind(tmp_path)

    record = store.load()[JargonStore.key_for("坏字段也保留条目")]

    assert record.raw_content == []
    assert record.chat_id_counts == [["chat-1", 2]]
    assert record.is_global is False
    assert record.count == 0
    assert record.is_jargon is True
    assert record.is_complete is False
    assert record.last_inference_count == 0
    assert record.updated_at > 0


def test_jargon_store_merges_concurrent_incremental_updates(tmp_path):
    seed = JargonStore()
    seed.bind(tmp_path)
    seed.save(
        [
            JargonRecord(
                content="梗",
                scope_chat_id="chat-1",
                raw_content=["base"],
                chat_id_counts=[["chat-1", 1]],
                count=1,
                updated_at=1.0,
            )
        ]
    )
    first = JargonStore()
    second = JargonStore()
    first.bind(tmp_path)
    second.bind(tmp_path)
    first_items = first.load()
    second_items = second.load()
    key = JargonStore.key_for("梗", "chat-1")

    first_items[key].count += 1
    first_items[key].raw_content.append("first")
    first_items[key].chat_id_counts = [["chat-1", 2]]
    first_items[key].updated_at = 2.0
    second_items[key].count += 1
    second_items[key].raw_content.append("second")
    second_items[key].chat_id_counts = [["chat-1", 2]]
    second_items[key].updated_at = 3.0

    first.save(list(first_items.values()))
    second.save(list(second_items.values()))

    reloaded = JargonStore()
    reloaded.bind(tmp_path)
    merged = reloaded.load()[key]
    assert merged.count == 3
    assert merged.raw_content == ["base", "first", "second"]
    assert merged.chat_id_counts == [["chat-1", 3]]
    assert merged.updated_at == 3.0


def test_jargon_store_same_directory_rebind_keeps_merge_baseline(tmp_path):
    store = JargonStore()
    store.bind(tmp_path)
    store.save([JargonRecord(content="梗", scope_chat_id="chat-1", count=1)])
    items = store.load()
    store.bind(tmp_path)
    items[JargonStore.key_for("梗", "chat-1")].count += 1

    concurrent = JargonStore()
    concurrent.bind(tmp_path)
    concurrent_items = concurrent.load()
    concurrent_items[JargonStore.key_for("梗", "chat-1")].count += 1
    concurrent.save(list(concurrent_items.values()))
    store.save(list(items.values()))

    reloaded = JargonStore()
    reloaded.bind(tmp_path)
    assert reloaded.load()[JargonStore.key_for("梗", "chat-1")].count == 3


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


def test_message_recorder_rebind_does_not_leak_previous_root_cache(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    recorder = MessageRecorder()
    recorder.bind(first_root)
    recorder.set_last_time("chat-1", 12.5)

    recorder.bind(second_root)
    assert recorder.get_last_time("chat-1") == 0.0

    recorder.bind(first_root)
    assert recorder.get_last_time("chat-1") == 12.5


def test_message_recorder_repairs_invalid_timestamp_values(tmp_path):
    learner_dir = tmp_path / "bw_learner"
    learner_dir.mkdir(parents=True)
    (learner_dir / "message_recorder.json").write_text(
        json.dumps(
            {
                "last_extraction_time": {
                    "nan": "NaN",
                    "infinite": "Infinity",
                    "negative": -1,
                    "valid": 9.5,
                }
            }
        ),
        encoding="utf-8",
    )

    recorder = MessageRecorder()
    recorder.bind(tmp_path)

    assert recorder.get_last_time("nan") == 0.0
    assert recorder.get_last_time("infinite") == 0.0
    assert recorder.get_last_time("negative") == 0.0
    assert recorder.get_last_time("valid") == 9.5


def test_message_recorder_uses_one_normalized_chat_key(tmp_path):
    recorder = MessageRecorder()
    recorder.bind(tmp_path)

    assert recorder.try_begin(" chat-1 ") is True
    assert recorder.try_begin("chat-1") is False
    recorder.end(" chat-1 ")
    assert recorder.try_begin("chat-1") is True
    recorder.end("chat-1")
