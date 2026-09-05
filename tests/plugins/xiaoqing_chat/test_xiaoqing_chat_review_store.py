"""XiaoQing Chat 反思会话存储的损坏恢复与结构校验回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat.memory import review_sessions
from plugins.xiaoqing_chat.memory.review_sessions import (
    ReviewPolicy,
    ReviewStore,
    maybe_push_session,
)


def _sessions_path(data_dir: Path) -> Path:
    return data_dir / "review_sessions" / "sessions.json"


def _session_payload(*, created_at: object = 1.0) -> dict[str, object]:
    return {
        "kind": "goal_strategy",
        "chat_id": "42",
        "created_at": created_at,
        "expires_at": 120.0,
        "step": 0,
        "last_push_ts": 0.0,
        "payload": {"goal": "自然聊天"},
        "answers": [],
    }


@pytest.mark.parametrize("value", ["²", "٣", "１２"])
def test_non_negative_int_rejects_unicode_digits(value: str) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        review_sessions._non_negative_int(value, field_name="step")


def test_review_store_recovers_malformed_primary_from_backup(tmp_path: Path) -> None:
    path = _sessions_path(tmp_path)
    path.parent.mkdir(parents=True)
    backup   = path.with_name(f"{path.name}.bak")
    expected = {
        "active": {"session-1": _session_payload()},
        "last_closed": {},
    }
    backup.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    path.write_text("{broken-json", encoding="utf-8")

    store = ReviewStore()
    store.bind(tmp_path)

    sessions = store.list_sessions()

    assert [session.session_id for session in sessions] == ["session-1"]
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert not list(path.parent.glob("sessions.json.corrupt-*"))


def test_review_store_quarantines_unrecoverable_primary_before_new_write(tmp_path: Path) -> None:
    path = _sessions_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = b"{broken-json"
    path.write_bytes(original)

    store = ReviewStore()
    store.bind(tmp_path)

    assert store.list_sessions() == []
    quarantined = list(path.parent.glob("sessions.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original
    assert not path.exists()

    created = store.open_session_if_allowed(
        kind             = "goal_strategy",
        chat_id          = "42",
        payload          = {},
        timeout_seconds  = 60,
        cooldown_seconds = 0,
        now              = 10.0,
    )

    assert created is not None
    assert path.is_file()
    assert quarantined[0].read_bytes() == original


def test_review_store_skips_invalid_entries_and_preserves_original_backup(tmp_path: Path) -> None:
    path = _sessions_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = {
        "active": {
            "valid": _session_payload(),
            "invalid": _session_payload(created_at="not-a-number"),
        },
        "last_closed": {"42:goal_strategy": 5.0, "bad": "not-a-number"},
    }
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    store = ReviewStore()
    store.bind(tmp_path)

    assert [session.session_id for session in store.list_sessions()] == ["valid"]
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert set(repaired["active"]) == {"valid"}
    assert repaired["last_closed"] == {"42:goal_strategy": 5.0}
    assert json.loads(path.with_name("sessions.json.bak").read_text(encoding="utf-8")) == original


def test_review_store_quarantines_malformed_policy_and_can_save_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "review_sessions" / "policies" / "42.json"
    path.parent.mkdir(parents=True)
    original = b"[broken-policy"
    path.write_bytes(original)

    store = ReviewStore()
    store.bind(tmp_path)

    assert store.get_policy("42") == ReviewPolicy()
    quarantined = list(path.parent.glob("42.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original

    store.save_policy(
        "42",
        ReviewPolicy(
            goal_override   = "保持简洁",
            goal_lock_until = 30.0,
            strategy_note   = "避免重复",
            avoid_patterns  = ["复读"],
        ),
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "goal_override": "保持简洁",
        "goal_lock_until": 30.0,
        "strategy_note": "避免重复",
        "avoid_patterns": ["复读"],
    }
    assert quarantined[0].read_bytes() == original


def test_review_store_rebind_clears_directory_specific_caches(tmp_path: Path) -> None:
    first       = tmp_path / "first"
    second      = tmp_path / "second"
    first_path  = _sessions_path(first)
    second_path = _sessions_path(second)
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_text(
        json.dumps({"active": {"first": _session_payload()}, "last_closed": {}}),
        encoding="utf-8",
    )
    second_path.write_text(
        json.dumps({"active": {"second": _session_payload()}, "last_closed": {}}),
        encoding="utf-8",
    )

    store = ReviewStore()
    store.bind(first)
    assert [session.session_id for session in store.list_sessions()] == ["first"]

    store.bind(second)
    assert [session.session_id for session in store.list_sessions()] == ["second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_pushed", "expected_timestamp"),
    [(False, False, 0.0), (None, True, 100.0)],
)
async def test_review_push_distinguishes_rejection_from_unknown_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: bool | None,
    expected_pushed: bool,
    expected_timestamp: float,
) -> None:
    store = ReviewStore()
    store.bind(tmp_path)
    session = store.open_session_if_allowed(
        kind             = "goal_strategy",
        chat_id          = "42",
        payload          = {"goal": "自然聊天"},
        timeout_seconds  = 60,
        cooldown_seconds = 0,
        now              = 10.0,
    )
    assert session is not None
    monkeypatch.setattr(review_sessions.time, "time", lambda: 100.0)

    async def send_action(_action: dict) -> bool | None:
        return outcome

    pushed = await maybe_push_session(
        context=SimpleNamespace(send_action=send_action),
        store                   = store,
        sess                    = session,
        operator_user_id        = 1,
        operator_group_id       = 0,
        resend_interval_seconds = 0,
    )

    assert pushed is expected_pushed
    assert session.last_push_ts == expected_timestamp
    assert store.get_session(session.session_id).last_push_ts == expected_timestamp
