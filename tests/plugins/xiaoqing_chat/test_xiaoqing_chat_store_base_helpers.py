# 验证共享存储工具的文件读写、备份与异常恢复。
from plugins.xiaoqing_chat import store_base as store_base_module
from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord
from plugins.xiaoqing_chat.store_base import StoreBase


class _DummyStore(StoreBase):
    pass


def test_store_base_load_json_from_path_parts_returns_default_when_missing(tmp_path):
    store = _DummyStore()
    store.bind(tmp_path)

    loaded = store._load_json_from_path_parts("bw_learner", "missing.json", default={"ok": True})

    assert loaded == {"ok": True}


def test_store_base_save_and_load_json_from_path_parts_roundtrip(tmp_path):
    store = _DummyStore()
    store.bind(tmp_path)

    saved = store._save_json_to_path_parts("bw_learner", "state.json", data={"k": 1})

    assert saved is True
    assert store._load_json_from_path_parts("bw_learner", "state.json", default={}) == {"k": 1}


def test_store_base_save_json_to_path_parts_returns_false_when_unbound():
    store = _DummyStore()

    assert store._save_json_to_path_parts("bw_learner", "state.json", data={"k": 1}) is False


def test_store_base_save_json_uses_atomic_writer(tmp_path, monkeypatch):
    store = _DummyStore()
    store.bind(tmp_path)
    captured = {}

    def _fake_write_json(path, data):
        captured["path"] = path
        captured["data"] = data
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(store_base_module, "write_json", _fake_write_json)

    assert store._save_json_to_path_parts("bw_learner", "state.json", data={"k": 1}) is True
    assert captured["data"] == {"k": 1}


def test_action_history_flush_uses_atomic_json_writer(tmp_path, monkeypatch):
    import plugins.xiaoqing_chat.planning.action_history as action_history_module

    captured = {}

    def _fake_write_json(path, data):
        captured["path"] = path
        captured["data"] = data

    monkeypatch.setattr(action_history_module, "write_json", _fake_write_json)

    store = ActionHistoryStore()
    store.bind(tmp_path)
    store.append(
        "chat-1",
        ActionRecord(
            ts           = 1.0,
            local_target = "t",
            action       = "reply",
            reasoning    = "ok",
            detail       = {},
            executed     = True,
        ),
    )

    store.flush("chat-1")

    assert captured["path"].name == "chat-1.json"
    assert captured["data"][0]["action"] == "reply"
