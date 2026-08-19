from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import aiohttp
import pytest

from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.expression import bw_expression_learner as expression_learner
from plugins.xiaoqing_chat.expression.bw_expression_reflector import (
    maybe_ask_for_reflection,
)
from plugins.xiaoqing_chat.expression.bw_expression_store import (
    ExpressionRecord,
    ExpressionStore,
)
from plugins.xiaoqing_chat.expression.bw_reflect_tracker import (
    ReflectTrackerStore,
    tick_reflect_tracker,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage


def _expression(expression_id: str, *, count: int = 1) -> ExpressionRecord:
    return ExpressionRecord(
        expression_id=expression_id,
        chat_id="g1",
        situation=f"situation-{expression_id}",
        style=f"style-{expression_id}",
        content_list=["base"],
        count=count,
        last_active_time=1.0,
    )


@pytest.mark.asyncio
async def test_expression_learning_filters_bot_lines_and_bounds_llm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    payload = [
        {
            "situation": "情境" * 50,
            "style": f"风格-{index}",
            "source_id": "m2",
        }
        for index in range(15)
    ]
    payload.insert(
        0,
        {"situation": "伪造来源", "style": "不应写入", "source_id": "missing"},
    )
    payload.append({"situation": "", "style": "invalid", "source_id": "bad"})

    async def complete(**kwargs):
        captured.update(kwargs)
        return (
            {"choices": [{"message": {"content": json.dumps(payload)}}]},
            "/v1/chat/completions",
        )

    monkeypatch.setattr(expression_learner, "chat_completions_raw_with_fallback_paths", complete)
    messages = [
        StoredMessage(role="assistant", name="bot", content="BOT_ONLY", ts=1),
        StoredMessage(role="user", name="user", content="USER_VISIBLE", ts=2, local_id="m2"),
    ]

    learned = await expression_learner.learn_from_messages(
        secrets={"api_base": "https://example.test", "api_key": "k", "model": "m"},
        messages=messages,
        temperature=9.0,
        top_p=0.9,
        max_tokens=100,
        timeout_seconds=1,
        max_retry=0,
        retry_interval_seconds=0,
    )

    assert len(learned) == 12
    assert all(len(item.situation) <= 80 for item in learned)
    assert all(item.situation != "伪造来源" for item in learned)
    prompt = captured["messages"][0]["content"]
    assert "USER_VISIBLE" in prompt
    assert "BOT_ONLY" not in prompt
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 500


@pytest.mark.asyncio
async def test_expression_upsert_caps_only_target_chat_and_preserves_other_tenant(tmp_path) -> None:
    store = ExpressionStore()
    store.bind(tmp_path)
    target = _expression("target")
    other = _expression("other")
    other.chat_id = "g2"
    store.save([target, other])

    changed = await expression_learner.upsert_learned(
        store=store,
        chat_id="g1",
        learned=[
            expression_learner.LearnedExpression(
                situation=target.situation,
                style=target.style,
                source_id="m1",
            ),
            expression_learner.LearnedExpression(
                situation="new situation",
                style="new style",
                source_id="m2",
            ),
        ],
        similarity_threshold=0.72,
        max_store=1,
        self_reflect=False,
        secrets={},
        bot_name="小青",
        personality=PersonalityConfig(),
        temperature=0.5,
        top_p=0.9,
        max_tokens=500,
        timeout_seconds=1,
        max_retry=0,
        retry_interval_seconds=0,
    )

    assert changed == 2
    saved = store.load()
    assert [item.expression_id for item in saved if item.chat_id == "g2"] == ["other"]
    scoped = [item for item in saved if item.chat_id == "g1"]
    assert len(scoped) == 1
    assert scoped[0].expression_id == "target"
    assert scoped[0].count == 2
    assert target.style in scoped[0].content_list


@pytest.mark.asyncio
async def test_expression_upsert_does_not_revive_user_rejected_situation(tmp_path) -> None:
    store = ExpressionStore()
    store.bind(tmp_path)
    rejected = _expression("rejected")
    rejected.rejected = True
    rejected.modified_by = "user"
    store.save([rejected])

    changed = await expression_learner.upsert_learned(
        store=store,
        chat_id="g1",
        learned=[
            expression_learner.LearnedExpression(
                situation=rejected.situation,
                style=rejected.style,
                source_id="m1",
            )
        ],
        similarity_threshold=0.72,
        max_store=20,
        self_reflect=False,
        secrets={},
        bot_name="小青",
        personality=PersonalityConfig(),
        temperature=0.5,
        top_p=0.9,
        max_tokens=500,
        timeout_seconds=1,
        max_retry=0,
        retry_interval_seconds=0,
    )

    assert changed == 0
    assert store.load() == [rejected]


def test_expression_store_concurrent_new_records_are_both_preserved(tmp_path) -> None:
    # 模拟热重载前后的两个类代际：旧实现的类级锁池在这里彼此独立。
    class FirstGenerationStore(ExpressionStore):
        _locks_guard = threading.Lock()
        _path_locks: ClassVar[dict[str, threading.RLock]] = {}

    class SecondGenerationStore(ExpressionStore):
        _locks_guard = threading.Lock()
        _path_locks: ClassVar[dict[str, threading.RLock]] = {}

    stores = [FirstGenerationStore(), SecondGenerationStore()]
    for store in stores:
        store.bind(tmp_path)
        assert store.load() == []

    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    active_reads = 0
    max_active_reads = 0

    for store in stores:
        original_read = store._read_records

        def delayed_read(original=original_read):
            nonlocal active_reads, max_active_reads
            with counter_lock:
                active_reads += 1
                max_active_reads = max(max_active_reads, active_reads)
            try:
                time.sleep(0.05)
                return original()
            finally:
                with counter_lock:
                    active_reads -= 1

        store._read_records = delayed_read

    def save(store: ExpressionStore, expression_id: str) -> None:
        barrier.wait(timeout=2)
        store.save([_expression(expression_id)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(save, stores[0], "first"),
            executor.submit(save, stores[1], "second"),
        ]
        for future in futures:
            future.result(timeout=5)

    assert max_active_reads == 1
    reloaded = ExpressionStore()
    reloaded.bind(tmp_path)
    assert {item.expression_id for item in reloaded.load()} == {"first", "second"}


def test_expression_store_rebases_concurrent_count_and_content_deltas(tmp_path) -> None:
    initial = ExpressionStore()
    initial.bind(tmp_path)
    initial.save([_expression("shared")])

    stores = [ExpressionStore(), ExpressionStore()]
    desired: list[list[ExpressionRecord]] = []
    for index, store in enumerate(stores):
        store.bind(tmp_path)
        items = store.load()
        items[0].count += 1
        items[0].content_list.append(f"addition-{index}")
        desired.append(items)

    barrier = threading.Barrier(2)

    def save(store: ExpressionStore, items: list[ExpressionRecord]) -> None:
        barrier.wait(timeout=2)
        store.save(items)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(save, store, items)
            for store, items in zip(stores, desired, strict=True)
        ]
        for future in futures:
            future.result(timeout=5)

    reloaded = ExpressionStore()
    reloaded.bind(tmp_path)
    record = reloaded.load()[0]
    assert record.count == 3
    assert set(record.content_list) == {"base", "addition-0", "addition-1"}


def test_expression_store_same_root_rebind_preserves_merge_baseline(tmp_path) -> None:
    initial = ExpressionStore()
    initial.bind(tmp_path)
    initial.save([_expression("shared")])

    first = ExpressionStore()
    first.bind(tmp_path)
    first_items = first.load()
    first_items[0].count += 1

    second = ExpressionStore()
    second.bind(tmp_path)
    second_items = second.load()
    second_items[0].count += 1
    second_items[0].style = "concurrent-style"
    second.save(second_items)

    # 真实消息路径会对同一个 RuntimeState 反复绑定同一个数据目录。
    first.bind(tmp_path)
    first.save(first_items)

    reloaded = ExpressionStore()
    reloaded.bind(tmp_path)
    record = reloaded.load()[0]
    assert record.count == 3
    assert record.style == "concurrent-style"

    first.bind(tmp_path / "other-root")
    assert first.load() == []


def test_expression_store_only_user_can_clear_a_rejection(tmp_path) -> None:
    store = ExpressionStore()
    store.bind(tmp_path)
    rejected = _expression("rejected")
    rejected.rejected = True
    rejected.modified_by = "user"
    store.save([rejected])

    ai_update = store.load()
    ai_update[0].checked = True
    ai_update[0].rejected = False
    ai_update[0].modified_by = "ai"
    store.save(ai_update)

    preserved = store.load()[0]
    assert preserved.checked is False
    assert preserved.rejected is True
    assert preserved.modified_by == "user"

    user_update = store.load()
    user_update[0].checked = True
    user_update[0].rejected = False
    user_update[0].modified_by = "user"
    store.save(user_update)

    reopened = store.load()[0]
    assert reopened.checked is True
    assert reopened.rejected is False
    assert reopened.modified_by == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retry", [0, 1, 2])
async def test_llm_retry_count_is_exact(monkeypatch: pytest.MonkeyPatch, max_retry: int) -> None:
    from core.ai import AIRequestError, complete_configured_route

    attempts = 0

    async def fail_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise aiohttp.ClientConnectionError("offline")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "core.ai.aiohttp_request_bounded",
        fail_request,
    )
    monkeypatch.setattr("core.ai.asyncio.sleep", no_sleep)

    config = {
        "ai": {
            "providers": {
                "test": {
                    "api_base": "https://example.com",
                    "endpoint_path": "/chat/completions",
                }
            },
            "models": {
                "test-model": {
                    "provider": "test",
                    "model": "model",
                    "modalities": ["text"],
                }
            },
        },
        "plugins": {
            "xiaoqing_chat": {
                "ai": {
                    "routes": {
                        "chat": {
                            "models": ["test-model"],
                            "max_retry": max_retry,
                            "retry_interval_seconds": 0.0,
                        }
                    }
                }
            }
        },
    }
    with pytest.raises(AIRequestError, match="ai_transport"):
        await complete_configured_route(
            session=object(),
            config=config,
            secrets={"ai": {"providers": {"test": {"api_key": "key"}}}},
            plugin_name="xiaoqing_chat",
            route_name="chat",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert attempts == 1 + max_retry


@pytest.mark.asyncio
async def test_reflector_queues_every_question_instead_of_overwriting(tmp_path) -> None:
    expression_store = ExpressionStore()
    expression_store.bind(tmp_path)
    expression_store.save([_expression("first"), _expression("second")])
    tracker_store = ReflectTrackerStore()
    actions: list[dict] = []

    async def send_action(action: dict) -> None:
        actions.append(action)

    context = SimpleNamespace(data_dir=tmp_path, send_action=send_action)
    sent = await maybe_ask_for_reflection(
        context=context,
        expr_store=expression_store,
        tracker_store=tracker_store,
        operator_user_id=0,
        operator_group_id=123,
        min_interval_seconds=0.0,
        ask_per_check=2,
    )

    assert sent == 2
    assert len(actions) == 2
    assert {state.expression_id for state in tracker_store.get_trackers("g123")} == {
        "first",
        "second",
    }


@pytest.mark.asyncio
async def test_reflector_does_not_advance_tracker_after_explicit_rejection(tmp_path) -> None:
    expression_store = ExpressionStore()
    expression_store.bind(tmp_path)
    expression_store.save([_expression("first")])
    tracker_store = ReflectTrackerStore()

    async def reject(_action: dict) -> bool:
        return False

    sent = await maybe_ask_for_reflection(
        context=SimpleNamespace(data_dir=tmp_path, send_action=reject),
        expr_store=expression_store,
        tracker_store=tracker_store,
        operator_user_id=0,
        operator_group_id=123,
        min_interval_seconds=0.0,
        ask_per_check=1,
    )

    assert sent == 0
    assert tracker_store.get_trackers("g123") == []


@pytest.mark.asyncio
async def test_reflection_reply_is_consumed_by_only_one_pending_question(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expression_store = ExpressionStore()
    expression_store.bind(tmp_path)
    expression_store.save([_expression("first"), _expression("second")])
    tracker_store = ReflectTrackerStore()
    tracker_store.bind(tmp_path)
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", lambda: 100.0
    )
    tracker_store.set_tracker("g1", "first")
    tracker_store.set_tracker("g1", "second")

    answer_one = StoredMessage(role="user", name="owner", content="同意第一条", ts=101.0)
    answer_two = StoredMessage(role="user", name="owner", content="同意第二条", ts=102.0)
    memory_store = SimpleNamespace(
        get_async=AsyncMock(side_effect=[[answer_one], [answer_one, answer_two]])
    )
    prompts: list[str] = []

    async def approve(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"judgment":"Approve",'
                                '"corrected_situation":"",'
                                '"corrected_style":""}'
                            )
                        }
                    }
                ]
            },
            "/v1/chat/completions",
        )

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_reflect_tracker."
        "chat_completions_raw_with_fallback_paths",
        approve,
    )
    request = {
        "operator_chat_id": "g1",
        "memory_store": memory_store,
        "expr_store": expression_store,
        "tracker_store": tracker_store,
        "secrets": {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        "bot_name": "小青",
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    assert await tick_reflect_tracker(**request) is True
    remaining = tracker_store.get_trackers("g1")
    assert [state.expression_id for state in remaining] == ["second"]
    assert remaining[0].last_consumed_time == 101.0

    assert await tick_reflect_tracker(**request) is True
    assert tracker_store.get_trackers("g1") == []
    assert "同意第一条" in prompts[0]
    assert "同意第一条" not in prompts[1]
    assert "同意第二条" in prompts[1]
    assert all(item.checked for item in expression_store.load())


@pytest.mark.asyncio
async def test_expired_reflection_removes_only_that_queue_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_store = ReflectTrackerStore()
    tracker_store.bind(tmp_path)
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", lambda: 100.0
    )
    tracker_store.set_tracker("g1", "first")
    tracker_store.set_tracker("g1", "second")
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", lambda: 200.0
    )

    changed = await tick_reflect_tracker(
        operator_chat_id="g1",
        memory_store=SimpleNamespace(get_async=AsyncMock()),
        expr_store=ExpressionStore(),
        tracker_store=tracker_store,
        secrets={},
        bot_name="小青",
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        max_duration_seconds=10.0,
    )

    assert changed is True
    assert [state.expression_id for state in tracker_store.get_trackers("g1")] == ["second"]
