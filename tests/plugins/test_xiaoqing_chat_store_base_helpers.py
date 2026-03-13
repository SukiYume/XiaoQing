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
