# 验证共享存储工具的文件读写，以及动作历史刷盘后落在会话文件里。
import json

import pytest

from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord
from plugins.xiaoqing_chat.store_base import StoreBase


def test_store_base_load_json_from_path_parts_returns_default_when_missing(tmp_path):
    store = StoreBase()
    store.bind(tmp_path)

    loaded = store._load_json_from_path_parts("bw_learner", "missing.json", default={"ok": True})

    assert loaded == {"ok": True}


def test_store_base_save_and_load_json_from_path_parts_roundtrip(tmp_path):
    store = StoreBase()
    store.bind(tmp_path)

    saved = store._save_json_to_path_parts("bw_learner", "state.json", data={"k": 1})

    assert saved is True
    assert store._load_json_from_path_parts("bw_learner", "state.json", default={}) == {"k": 1}


def test_store_base_save_json_to_path_parts_returns_false_when_unbound():
    store = StoreBase()

    assert store._save_json_to_path_parts("bw_learner", "state.json", data={"k": 1}) is False


@pytest.mark.asyncio
async def test_action_history_flush_persists_records_to_the_chat_file(tmp_path):
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

    on_disk = json.loads((tmp_path / "action_history" / "chat-1.json").read_text(encoding="utf-8"))
    assert on_disk[0]["action"] == "reply"
    reloaded = ActionHistoryStore()
    reloaded.bind(tmp_path)
    assert [record.action for record in await reloaded.get_recent_async("chat-1")] == ["reply"]
