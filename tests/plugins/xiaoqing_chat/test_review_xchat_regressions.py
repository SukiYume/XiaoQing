"""完整审查中确认的生命周期、并发快照和数据恢复回归。"""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord, ExpressionStore
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt
from plugins.xiaoqing_chat.media import emoji_library
from plugins.xiaoqing_chat.media.event_media_common import RenderedMedia
from plugins.xiaoqing_chat.memory.knowledge_extract import PersonFact, _persist_person_facts
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
from plugins.xiaoqing_chat.memory.person_profile import (
    clear_profiles_and_memory,
    get_profile_generation,
    load_profile,
    update_profile_and_index,
)
from plugins.xiaoqing_chat.memory.review_sessions import _load_json_document
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
from tests.helpers.xiaoqing_chat_media_test_support import _make_media_runtime


@pytest.mark.asyncio
async def test_shutdown_reports_live_cleanup(monkeypatch):
    from plugins.xiaoqing_chat import main

    task = Mock()
    state = SimpleNamespace(stop_accepting_background_tasks=Mock(), background_tasks=lambda: {task})
    monkeypatch.setattr(main, "_state", lambda: state)
    monkeypatch.setattr(main, "_flush_shutdown_state", AsyncMock())
    monkeypatch.setattr(main.asyncio, "wait", AsyncMock(return_value=(set(), {task})))
    with pytest.raises(TimeoutError, match="still running"):
        await main.shutdown(SimpleNamespace(logger=Mock()))
    task.cancel.assert_called_once()


@pytest.mark.parametrize("primary", ["[]", "not json", '{"version": 99}'])
def test_structural_corruption_recovers_valid_backup(tmp_path, primary):
    path = tmp_path / "policy.json"
    path.write_text(primary, encoding="utf-8")
    backup = path.with_suffix(".json.bak")
    backup.write_text('{"version": 1, "goal": "keep"}', encoding="utf-8")
    original = backup.read_bytes()

    def normalize(value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid schema")
        return value, False

    result = _load_json_document(
        path, description="test", default_factory=dict, normalize=normalize
    )
    assert result["goal"] == "keep"
    assert path.read_bytes() == backup.read_bytes() == original


@pytest.mark.asyncio
async def test_evicted_borrowed_lock_and_waiter_keep_identity(monkeypatch):
    monkeypatch.setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 1)
    state = ChatRuntimeState()
    old   = state.get_lock("old")
    state.set_last_observe_ts("new", 100)
    state.cleanup_stale_chats()
    assert state.get_lock("old") is old
    await old.acquire()
    waiter = asyncio.create_task(old.acquire())
    await asyncio.sleep(0)
    old.release()
    state.cleanup_stale_chats()
    assert state.get_lock("old") is old
    await waiter
    old.release()


def test_quarantined_memory_cannot_be_queried(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id="bad", text="private fact", meta={"type": "quarantined_person_info", "chat_id": "g1"}
    )
    db.upsert_text(
        doc_id="good", text="private fact", meta={"type": "person_profile", "chat_id": "g1"}
    )
    assert [item.doc_id for item in db.query("private fact", chat_id="g1", min_score=0)] == ["good"]


def test_local_ids_survive_restarts_and_idle_reset(tmp_path):
    for index in range(3):
        state = ChatRuntimeState()
        state.memory_store.bind_data_dir(tmp_path)
        number = state.fetch_and_increment_local_id("g1")
        assert number == index + 1
        state.memory_store.append(
            "g1", role="user", name="u", content=str(index), local_id=f"m{number}"
        )
        state.memory_store.persist("g1")
        state.clear_transient_chat_state("g1")
        assert state.fetch_and_increment_local_id("g1") == index + 2
    assert len(state.memory_store.get("g1")) == 3


def test_profile_reset_clears_backups_and_rejects_old_generation(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    generation = get_profile_generation(tmp_path, "g1")
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["old"],
    )
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["older"],
    )
    clear_profiles_and_memory(tmp_path, "g1", db)
    _persist_person_facts(
        data_dir            = tmp_path,
        memory_db           = db,
        chat_id             = "g1",
        facts               = [PersonFact(1, "u", "stale", "stale")],
        expected_generation = generation,
    )
    assert load_profile(tmp_path, chat_id="g1", subject_id=1) is None
    assert not list((tmp_path / "person_profiles" / "g1").glob("*.json*"))
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["new"],
    )
    assert load_profile(tmp_path, chat_id="g1", subject_id=1).facts == ["new"]


def test_history_budget_keeps_latest_correction():
    history = [StoredMessage("user", "u", i + 1, content="old" * 200) for i in range(11)]
    history.append(StoredMessage("user", "u", 20, content="LATEST_STOP_PLEASE"))
    prompt = build_dialogue_prompt(history, bot_name="小青", max_chars=250)
    assert "LATEST_STOP_PLEASE" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds,expected", [(15, 15), (99, 30), (-4, 0), (True, 0), ("15", 0), (None, 0)]
)
async def test_planner_preserves_optional_wait(monkeypatch, seconds, expected):
    from plugins.xiaoqing_chat.planning import pfc_action_planner as planner

    response = {
        "action": "wait",
        "reason": "wait",
        "thinking": "user typing",
        "wait_seconds": seconds,
    }
    fake = AsyncMock(
        return_value=({"choices": [{"message": {"content": json.dumps(response)}}]}, "test")
    )
    monkeypatch.setattr(planner, "chat_completions_raw_with_fallback_paths", fake)
    plan = await planner.plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = False,
        personality                  = PersonalityConfig(),
        history                      = [],
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        temperature                  = 0.5,
        top_p                        = 0.9,
        max_tokens                   = 300,
        timeout_seconds              = 1,
        max_retry                    = 0,
        retry_interval_seconds       = 0,
        current_text                 = "",
    )
    assert plan.wait_seconds == expected
    assert plan.thinking == "user typing"


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected,modified", [(False, ""), (True, ""), (False, "revised")])
async def test_expression_review_allows_empty_modifications(monkeypatch, rejected, modified):
    from plugins.xiaoqing_chat.expression import bw_expression_learner as learner

    response = {
        "checked": True,
        "rejected": rejected,
        "reason": "reviewed",
        "modified_situation": modified,
        "modified_style": "",
    }
    monkeypatch.setattr(
        learner,
        "chat_completions_raw_with_fallback_paths",
        AsyncMock(
            return_value=({"choices": [{"message": {"content": json.dumps(response)}}]}, "test")
        ),
    )
    result = await learner.single_expression_check(
        secrets                = {},
        bot_name               = "小青",
        personality            = PersonalityConfig(),
        situation              = "s",
        style                  = "t",
        temperature            = 0.5,
        top_p                  = 0.9,
        max_tokens             = 300,
        timeout_seconds        = 1,
        max_retry              = 0,
        retry_interval_seconds = 0,
    )
    assert result[:2] == (True, rejected)
    assert result[3] == modified


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expression", "jargon"])
async def test_shared_store_interleaved_snapshots_keep_both_chats(tmp_path, kind):
    store = ExpressionStore() if kind == "expression" else JargonStore()
    store.bind(tmp_path)
    loaded = asyncio.Event()
    saved  = asyncio.Event()

    async def worker(chat):
        items = store.load()
        if chat == "gA":
            loaded.set()
            await saved.wait()
        else:
            await loaded.wait()
        if kind == "expression":
            items.append(ExpressionRecord(chat, chat, "s", "t"))
        else:
            items[chat] = JargonRecord(chat, scope_chat_id=chat)
        await asyncio.to_thread(store.save, items)
        # 同一份快照重试保存时，已经提交的计数增量保持幂等。
        await asyncio.to_thread(store.save, items)
        if chat == "gB":
            saved.set()

    await asyncio.gather(worker("gA"), worker("gB"))
    assert len(store.load()) == 2


def _collect(context, runtime, path, color, chat="g1"):
    Image.new("RGB", (24, 24), color).save(path)
    rendered = RenderedMedia(
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "emoji",
        color,
        ("开心",),
        f"[表情包：{color}]",
    )
    return emoji_library.collect_emoji_candidate(
        context, runtime, rendered, source_path=path, source_chat_id=chat
    )


def test_similar_emoji_metadata_matches_retained_image(tmp_path):
    context = SimpleNamespace(data_dir=tmp_path)
    runtime = _make_media_runtime(enable_auto_collect_inbound_emoji=True)
    first, _ = _collect(context, runtime, tmp_path / "red.png", "red")
    second, created = _collect(context, runtime, tmp_path / "blue.png", "blue")
    assert not created
    assert second.description == first.description == "red"
    assert second.media_hash == first.media_hash
    with Image.open(emoji_library.resolve_emoji_file_path(context, second.file_path)) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


def test_concurrent_emoji_collect_and_stale_repair_preserve_updates(tmp_path):
    context = SimpleNamespace(data_dir=tmp_path)
    runtime = _make_media_runtime(enable_auto_collect_inbound_emoji=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(_collect, context, runtime, tmp_path / f"{color}.png", color, chat)
            for color, chat in [("red", "g1"), ("blue", "g2")]
        ]
        entries = [job.result(timeout=5)[0] for job in jobs]
    original = emoji_library._load_index(context)
    assert len(original["entries"]) == 2
    desired                                                  = deepcopy(original)
    desired["entries"][entries[0].media_hash]["description"] = "repair"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: emoji_library.mark_emoji_used(context, entries[0]), range(10)))
    emoji_library._save_index_if_changed(context, original_payload=original, payload=desired)
    latest = emoji_library._load_index(context)
    assert len(latest["entries"]) == 2
    assert latest["entries"][entries[0].media_hash]["usage_count"] == 10
