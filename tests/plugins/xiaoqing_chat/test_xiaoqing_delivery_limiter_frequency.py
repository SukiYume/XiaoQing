from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.app import XiaoQingApp
from core.delivery import (
    DELIVERY_RECEIPT_KEY,
    DeliveryReceipt,
    DeliverySegments,
    attach_receipt,
)
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.frequency_control import _freq_record, _should_reply_decision
from plugins.xiaoqing_chat.generation_limiter import (
    GenerationLimiter,
    GenerationLimitExceeded,
)
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState


def _bare_app(*, outcomes) -> XiaoQingApp:
    app = object.__new__(XiaoQingApp)
    app._maybe_split_action = MagicMock(side_effect=lambda action: [action])
    app._send_single_action = AsyncMock(side_effect=outcomes)
    app._notify_outgoing_action_observers = AsyncMock()
    return app


def _receipt(expected_actions: int = 1):
    committed = AsyncMock()
    rolled_back = AsyncMock()
    unknown = AsyncMock()
    return (
        DeliveryReceipt(
            expected_actions=expected_actions,
            commit=committed,
            rollback=rolled_back,
            unknown=unknown,
        ),
        committed,
        rolled_back,
        unknown,
    )


def test_delivery_receipt_serializes_expected_action_updates() -> None:
    """同步扩容与异步交付共享同一把锁，避免跨线程状态竞态。"""

    receipt, _commit, _rollback, _unknown = _receipt()
    entered = threading.Event()
    finished = threading.Event()

    def add_action() -> None:
        entered.set()
        receipt.add_expected_actions(1)
        finished.set()

    receipt._lock.acquire()
    worker = threading.Thread(target=add_action)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        assert finished.wait(timeout=0.05) is False
    finally:
        receipt._lock.release()
        worker.join(timeout=1)

    assert finished.is_set()
    assert receipt.expected_actions == 2


@pytest.mark.asyncio
async def test_send_action_commits_only_after_delivery_ack() -> None:
    receipt, commit, rollback, unknown = _receipt()
    app = _bare_app(outcomes=[True])
    action = attach_receipt(
        {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
        receipt,
    )

    assert await XiaoQingApp._send_action(app, action) is True

    commit.assert_awaited_once()
    rollback.assert_not_awaited()
    unknown.assert_not_awaited()
    assert receipt.committed is True


@pytest.mark.asyncio
async def test_send_action_preserves_unknown_receipt_outcome() -> None:
    receipt, commit, rollback, unknown = _receipt()
    app = _bare_app(outcomes=[None])
    action = attach_receipt(
        {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
        receipt,
    )

    assert await XiaoQingApp._send_action(app, action) is None

    commit.assert_not_awaited()
    rollback.assert_not_awaited()
    unknown.assert_awaited_once()
    assert receipt.resolved is True
    assert receipt.committed is False
    assert receipt.outcome is None


@pytest.mark.asyncio
async def test_observer_cancellation_cannot_rollback_delivered_reply() -> None:
    receipt, commit, rollback, _unknown = _receipt()
    app = _bare_app(outcomes=[True])
    app._notify_outgoing_action_observers = AsyncMock(side_effect=asyncio.CancelledError)
    action = attach_receipt(
        {
            "action": "send_group_msg",
            "params": {"group_id": 1, "message": []},
            "_source_plugin": "example",
        },
        receipt,
    )

    with pytest.raises(asyncio.CancelledError):
        await app._send_action(action)

    commit.assert_awaited_once()
    rollback.assert_not_awaited()
    assert receipt.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [False, asyncio.TimeoutError("timeout")])
async def test_send_action_rolls_back_rejection_and_timeout(outcome) -> None:
    receipt, commit, rollback, _unknown = _receipt()
    app = _bare_app(outcomes=[outcome])
    action = attach_receipt(
        {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
        receipt,
    )

    if isinstance(outcome, BaseException):
        with pytest.raises(asyncio.TimeoutError):
            await XiaoQingApp._send_action(app, action)
    else:
        assert await XiaoQingApp._send_action(app, action) is False

    commit.assert_not_awaited()
    rollback.assert_awaited_once()
    assert receipt.committed is False


@pytest.mark.asyncio
async def test_partial_multiaction_delivery_rolls_back_logical_reply() -> None:
    receipt, commit, rollback, _unknown = _receipt()
    app = _bare_app(outcomes=[True, False])
    actions = [
        attach_receipt(
            {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
            receipt,
        ),
        attach_receipt(
            {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
            receipt,
        ),
    ]
    app._maybe_split_action = MagicMock(return_value=actions)

    assert await XiaoQingApp._send_action(app, actions[0]) is False

    commit.assert_not_awaited()
    rollback.assert_awaited_once()
    assert receipt.expected_actions == 2


@pytest.mark.asyncio
async def test_delivery_receipt_never_reaches_onebot_transport() -> None:
    receipt, _commit, _rollback, _unknown = _receipt()
    app = object.__new__(XiaoQingApp)
    ws_client = MagicMock()
    ws_client.connected.return_value = True
    ws_client.send_action = AsyncMock(return_value=True)
    app.ws_client = ws_client
    app.inbound_manager = None
    app.http_sender = None
    app._ws_transport_is_trusted = MagicMock(return_value=True)
    action = attach_receipt(
        {"action": "send_group_msg", "params": {"group_id": 1, "message": []}},
        receipt,
    )

    assert await XiaoQingApp._send_single_action(app, action) is True

    delivered_action = ws_client.send_action.await_args.args[0]
    assert DELIVERY_RECEIPT_KEY not in delivered_action


@pytest.mark.asyncio
async def test_process_event_carries_delivery_receipt_to_action() -> None:
    receipt, _commit, _rollback, _unknown = _receipt()
    app = object.__new__(XiaoQingApp)
    app._stopping = False
    app.dispatcher = SimpleNamespace(
        handle_event=AsyncMock(
            return_value=DeliverySegments([{"type": "text", "data": {"text": "hello"}}], receipt)
        )
    )

    action = await XiaoQingApp._process_event(app, {"user_id": 2, "group_id": 1, "message": []})

    assert action is not None
    assert action[DELIVERY_RECEIPT_KEY] is receipt


@pytest.mark.asyncio
async def test_generation_limiter_sweeps_expired_unique_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100_000.0]
    monkeypatch.setattr("plugins.xiaoqing_chat.generation_limiter.time.time", lambda: clock[0])
    limiter = GenerationLimiter(max_tracked_users=3, sweep_interval_seconds=0.0)
    kwargs = {
        "max_global": 0,
        "max_per_chat": 0,
        "max_per_user": 0,
        "max_calls_per_user_per_day": 10,
    }
    for index in range(3):
        async with limiter.admit(chat_id=f"g{index}", user_id=f"u{index}", **kwargs):
            pass
    assert len(limiter._user_calls) == 3

    clock[0] += 86_401.0
    async with limiter.admit(chat_id="fresh", user_id="fresh", **kwargs):
        pass

    assert set(limiter._user_calls) == {"fresh"}


@pytest.mark.asyncio
async def test_generation_limiter_rejects_new_keys_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.xiaoqing_chat.generation_limiter.time.time", lambda: 100_000.0)
    limiter = GenerationLimiter(max_tracked_users=2, sweep_interval_seconds=0.0)
    kwargs = {
        "max_global": 0,
        "max_per_chat": 0,
        "max_per_user": 0,
        "max_calls_per_user_per_day": 10,
    }
    for user_id in ("u1", "u2"):
        async with limiter.admit(chat_id=user_id, user_id=user_id, **kwargs):
            pass

    with pytest.raises(GenerationLimitExceeded, match="daily_user_capacity"):
        async with limiter.admit(chat_id="g3", user_id="u3", **kwargs):
            pass

    assert len(limiter._user_calls) == 2


@pytest.mark.asyncio
async def test_continuous_reply_limit_blocks_exact_next_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = XiaoQingChatConfig(
        continuous_reply_limit=2,
        continuous_cooldown_seconds=10.0,
        min_reply_interval_seconds=0.0,
        max_replies_per_minute=0,
        reply_probability_base=1.0,
    )
    cfg.goal.enable_goal = False
    runtime = SimpleNamespace(cfg=cfg)
    state = ChatRuntimeState()
    monkeypatch.setattr("plugins.xiaoqing_chat.frequency_control.time.time", lambda: 100.0)

    _freq_record("g1", runtime, state, forced=False)
    assert state.get_continuous_reply_count("g1") == 1
    assert state.get_continuous_cooldown_until("g1") == 0.0

    _freq_record("g1", runtime, state, forced=False)
    assert state.get_continuous_reply_count("g1") == 0
    assert state.get_continuous_cooldown_until("g1") == 110.0
    decision = await _should_reply_decision(runtime, state, "g1", "hello", False, False)
    assert decision.should_reply is False
    assert decision.reason == "continuous_cooldown"

    monkeypatch.setattr("plugins.xiaoqing_chat.frequency_control.time.time", lambda: 111.0)
    decision = await _should_reply_decision(runtime, state, "g1", "hello", False, False)
    assert decision.should_reply is True
