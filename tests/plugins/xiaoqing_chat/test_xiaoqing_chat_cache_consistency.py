"""话题摘要与动作历史缓存的一致性测试。"""

import asyncio
from pathlib import Path

import pytest


def test_topic_summary_cache_parser_keeps_latest_valid_topic_summary(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.memory.topic_summary_cache import load_topic_summary_entries

    chat_id = "shared-cache-parser"
    summary_path = tmp_path / "hippo_memorizer" / f"{chat_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    _ = summary_path.write_text(
        (
            "["
            '{"topic_id":"t1","topic":"旧话题","summary":"旧摘要","updated_at":1},'
            '{"topic":"火锅选择","summary":"先看预算和口味","updated_at":2},'
            '{"topic":"","summary":""}'
            "]"
        ),
        encoding="utf-8",
    )

    entries = load_topic_summary_entries(tmp_path, chat_id)

    assert len(entries) == 2
    assert entries[-1].topic == "火锅选择"
    assert entries[-1].summary == "先看预算和口味"


def test_summarizer_load_cache_accepts_legacy_entries_without_topic_id(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.llm.summarizer import _load_cache

    chat_id = "legacy-summarizer-cache"
    summary_path = tmp_path / "hippo_memorizer" / f"{chat_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    _ = summary_path.write_text(
        (
            "["
            '{"topic_id":"t1","topic":"旧话题","summary":"旧摘要","updated_at":1},'
            '{"topic":"火锅选择","summary":"先看预算和口味","updated_at":2}'
            "]"
        ),
        encoding="utf-8",
    )

    topics = _load_cache(tmp_path, chat_id)

    assert len(topics) == 2
    assert topics[-1].topic == "火锅选择"


@pytest.mark.asyncio
async def test_action_history_append_on_cold_cache_keeps_persisted_records(
    tmp_path: Path,
) -> None:
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord

    chat_id = "cold-cache-history"
    store = ActionHistoryStore()
    store.bind(tmp_path)

    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    _ = (action_dir / f"{chat_id}.json").write_text(
        (
            "["
            '{"ts":1.0,"local_target":"u1","action":"wait","reasoning":"persisted","detail":{},"executed":true}'
            "]"
        ),
        encoding="utf-8",
    )

    store.append(
        chat_id,
        ActionRecord(
            ts=2.0,
            local_target="u2",
            action="reply",
            reasoning="new-record",
            detail={},
            executed=True,
        ),
    )
    store.flush(chat_id)

    reloaded = ActionHistoryStore()
    reloaded.bind(tmp_path)
    records = await reloaded.get_recent_async(chat_id, max_items=10)

    assert len(records) == 2
    assert records[0].reasoning == "persisted"
    assert records[1].reasoning == "new-record"


@pytest.mark.asyncio
async def test_action_history_get_recent_async_does_not_overwrite_concurrent_append(
    tmp_path: Path,
) -> None:
    from plugins.xiaoqing_chat.planning import action_history as action_history_module
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord

    chat_id = "append-race-history"
    store = ActionHistoryStore()
    store.bind(tmp_path)

    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    _ = (action_dir / f"{chat_id}.json").write_text(
        (
            "["
            '{"ts":1.0,"local_target":"u1","action":"wait","reasoning":"persisted","detail":{},"executed":true}'
            "]"
        ),
        encoding="utf-8",
    )

    gate = asyncio.Event()
    original_to_thread = action_history_module.asyncio.to_thread

    async def delayed_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "_load":
            await gate.wait()
        return await original_to_thread(func, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(action_history_module.asyncio, "to_thread", delayed_to_thread)
        pending = asyncio.create_task(store.get_recent_async(chat_id, max_items=10))
        await asyncio.sleep(0)
        store.append(
            chat_id,
            ActionRecord(
                ts=2.0,
                local_target="u2",
                action="reply",
                reasoning="new-record",
                detail={},
                executed=True,
            ),
        )
        gate.set()
        records = await pending

    assert len(records) == 1
    assert records[0].reasoning == "new-record"
    store.flush(chat_id)

    reloaded = ActionHistoryStore()
    reloaded.bind(tmp_path)
    persisted = await reloaded.get_recent_async(chat_id, max_items=10)
    assert len(persisted) == 1
    assert persisted[0].reasoning == "new-record"


@pytest.mark.asyncio
async def test_action_history_get_recent_async_does_not_restore_after_concurrent_clear(
    tmp_path: Path,
) -> None:
    from plugins.xiaoqing_chat.planning import action_history as action_history_module
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    chat_id = "clear-race-history"
    store = ActionHistoryStore()
    store.bind(tmp_path)

    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    _ = (action_dir / f"{chat_id}.json").write_text(
        (
            "["
            '{"ts":1.0,"local_target":"u1","action":"wait","reasoning":"persisted","detail":{},"executed":true}'
            "]"
        ),
        encoding="utf-8",
    )

    gate = asyncio.Event()
    original_to_thread = action_history_module.asyncio.to_thread

    async def delayed_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "_load":
            await gate.wait()
        return await original_to_thread(func, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(action_history_module.asyncio, "to_thread", delayed_to_thread)
        pending = asyncio.create_task(store.get_recent_async(chat_id, max_items=10))
        await asyncio.sleep(0)
        store.clear(chat_id)
        gate.set()
        records = await pending

    assert records == []
    store.flush(chat_id)
    reloaded = ActionHistoryStore()
    reloaded.bind(tmp_path)
    assert await reloaded.get_recent_async(chat_id, max_items=10) == []
