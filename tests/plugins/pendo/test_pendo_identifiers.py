"""Pendo 内部 UUID、公开短标识和唯一解析的回归测试。"""

from __future__ import annotations

import re

import pytest

from plugins.pendo.core.exceptions import AmbiguousIdentifierException
from plugins.pendo.models.item import DiaryItem, EventItem, LedgerItem, NoteItem, TaskItem
from plugins.pendo.services.db import Database
from plugins.pendo.services.event_graph import EventGraphService
from plugins.pendo.services.reminder import ReminderService
from plugins.pendo.utils.identifiers import (
    is_canonical_internal_id,
    public_id,
    public_id_matches,
)


def test_every_new_item_type_uses_full_uuid_and_eight_character_display_id() -> None:
    items = [EventItem(), TaskItem(), NoteItem(), DiaryItem(), LedgerItem()]

    assert len({item.id for item in items}) == len(items)
    assert all(is_canonical_internal_id(item.id) for item in items)
    assert all(re.fullmatch(r"[0-9a-f]{8}", item.display_id) for item in items)
    assert all(item.display_id == item.id[:8] for item in items)


@pytest.mark.parametrize(
    ("stored_id", "expected"),
    [
        ("5FAE8AC89F474F53B191966934C2C00D", "5fae8ac8"),
        ("5fae8ac89f474f53b191966934c2c00d", "5fae8ac8"),
        (
            "5fae8ac8-9f47-4f53-b191-966934c2c00d",
            "5fae8ac8-9f47-4f53-b191-966934c2c00d",
        ),
        (
            "5fae8ac89f474f53b191966934c2c00d_20260825",
            "5fae8ac89f474f53b191966934c2c00d_20260825",
        ),
        ("legacy-event-id", "legacy-event-id"),
    ],
)
def test_public_id_only_shortens_canonical_internal_uuid(stored_id: str, expected: str) -> None:
    assert public_id(stored_id) == expected
    assert public_id_matches(stored_id, expected)


def test_database_resolves_short_ids_per_owner_and_mutates_canonical_row(db: Database) -> None:
    owner_a   = "uuid-owner-a"
    owner_b   = "uuid-owner-b"
    item_a_id = "01234567" + "a" * 24
    item_b_id = "01234567" + "b" * 24
    db.insert_item(NoteItem(id=item_a_id, owner_id=owner_a, title="A"))
    db.insert_item(NoteItem(id=item_b_id, owner_id=owner_b, title="B"))

    assert db.resolve_item_id(owner_a, "01234567") == item_a_id
    assert db.resolve_item_id(owner_b, "01234567") == item_b_id
    assert db.update_item("01234567", {"title": "A2"}, owner_id=owner_a)
    assert db.get_item(item_a_id, owner_id=owner_a).title == "A2"
    assert db.get_item(item_b_id, owner_id=owner_b).title == "B"


def test_database_default_event_collection_id_uses_full_uuid(db: Database) -> None:
    collection_id = db.create_event_collection(
        {
            "owner_id": "uuid-default-collection-owner",
            "kind": "multi_node",
            "title": "默认集合 ID",
        }
    )

    assert is_canonical_internal_id(collection_id)
    assert public_id(collection_id) == collection_id[:8]


def test_database_rejects_ambiguous_short_id_without_touching_rows(db: Database) -> None:
    owner_id  = "uuid-ambiguous-owner"
    first_id  = "deadbeef" + "0" * 24
    second_id = "deadbeef" + "1" * 24
    db.insert_item(NoteItem(id=first_id, owner_id=owner_id, title="first"))
    db.insert_item(NoteItem(id=second_id, owner_id=owner_id, title="second"))

    with pytest.raises(AmbiguousIdentifierException) as captured:
        db.resolve_item_id(owner_id, "deadbeef")
    assert captured.value.matched_ids == [first_id, second_id]

    with pytest.raises(AmbiguousIdentifierException):
        db.update_item("deadbeef", {"title": "wrong"}, owner_id=owner_id)
    assert db.get_item(first_id, owner_id=owner_id).title == "first"
    assert db.get_item(second_id, owner_id=owner_id).title == "second"


def test_event_graph_rejects_short_id_shared_by_leaf_and_collection(db: Database) -> None:
    owner_id      = "uuid-event-namespace-owner"
    leaf_id       = "badc0ffe" + "0" * 24
    collection_id = "badc0ffe" + "1" * 24
    db.insert_item(EventItem(id=leaf_id, owner_id=owner_id, title="叶子"))
    db.create_event_collection(
        {
            "id": collection_id,
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "集合",
        }
    )

    with pytest.raises(AmbiguousIdentifierException):
        EventGraphService(db).load_by_id(owner_id, "badc0ffe")


def test_reminder_commands_show_short_id_and_confirmation_resolves_it(db: Database) -> None:
    owner_id    = "uuid-reminder-owner"
    item_id     = "facefeed" + "2" * 24
    remind_time = "2035-08-25T18:00:00+00:00"
    event       = EventItem(
        id           = item_id,
        owner_id     = owner_id,
        title        = "UUID 提醒",
        start_time   = remind_time,
        remind_times = [remind_time],
    )
    db.insert_item(event)

    message = ReminderService(db)._build_reminder_message(event, remind_time)
    assert f"/pendo confirm {event.display_id}" in message
    assert f"/pendo snooze {event.display_id} 10m" in message
    assert item_id not in message

    db.confirm_reminder(
        event.display_id,
        owner_id     = owner_id,
        remind_time  = remind_time,
        allow_future = True,
    )
    logs = db.get_reminder_logs(item_id)
    assert len(logs) == 1
    assert logs[0]["confirmed_at"] is not None


def test_web_returns_conflict_for_ambiguous_short_id(client, db: Database) -> None:
    from plugins.pendo.web.deps import get_current_user

    owner_id  = "uuid-web-ambiguous-owner"
    first_id  = "decafbad" + "0" * 24
    second_id = "decafbad" + "1" * 24
    db.insert_item(NoteItem(id=first_id, owner_id=owner_id, title="first"))
    db.insert_item(NoteItem(id=second_id, owner_id=owner_id, title="second"))
    client.app.dependency_overrides[get_current_user] = lambda: owner_id
    try:
        response = client.get("/api/items/decafbad")
    finally:
        client.app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 409
    payload = response.json()
    assert payload["ok"] is False
    assert "请使用完整 ID" in payload["message"]
    assert first_id in payload["message"]
    assert second_id in payload["message"]
