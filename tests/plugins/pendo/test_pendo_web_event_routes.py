"""Pendo Web 日程集合路由的规范化、事务和审计回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from plugins.pendo.core.exceptions import ItemNotFoundException
from plugins.pendo.services.db import Database
from plugins.pendo.web.api import events as events_api


def _create_collection(
    db: Database,
    owner_id: str,
    *,
    title: str                                  = "发布项目",
    reminder_rules: list[dict[str, int]] | None = None,
) -> tuple[str, list[str]]:
    """通过公开路由创建供更新、删除用例复用的两节点集合。"""

    created = events_api.create_event_collection(
        body=events_api.EventCollectionCreate(
            title          = title,
            reminder_rules = (
                reminder_rules if reminder_rules is not None else [{"offset_seconds": 0}]
            ),
            children=[
                events_api.EventCollectionChildCreate(
                    title      = "提审",
                    start_time = "2030-05-01T10:00:00",
                ),
                events_api.EventCollectionChildCreate(
                    title      = "上线",
                    start_time = "2030-05-02T18:00:00",
                ),
            ],
        ),
        owner_id = owner_id,
        db       = db,
    )
    data = cast(dict[str, Any], created["data"])
    return str(data["id"]), cast(list[str], data["child_ids"])


def _operation_rows(db: Database, owner_id: str, action: str) -> list[dict[str, Any]]:
    """读取指定动作的原始审计行，便于验证次数和事务内容。"""

    rows = db.get_connection().execute(
        """
        SELECT action, item_type, item_id, details
        FROM operation_logs
        WHERE user_id = ? AND action = ?
        ORDER BY id
        """,
        (owner_id, action),
    )
    return [dict(row) for row in rows.fetchall()]


def test_event_router_registers_only_the_expected_collection_and_detail_paths() -> None:
    """路由模块应保持职责明确的日程入口。"""

    registered = {
        (route.path, frozenset(getattr(route, "methods", set())))
        for route in events_api.router.routes
    }

    assert registered == {
        ("/events/overview", frozenset({"GET"})),
        ("/events/categories", frozenset({"GET"})),
        ("/events/collections", frozenset({"POST"})),
        ("/events/collections/{collection_id}/detail", frozenset({"GET"})),
        ("/events/collections/{collection_id}", frozenset({"PUT"})),
        ("/events/collections/{collection_id}", frozenset({"DELETE"})),
        ("/events/{event_id}/detail", frozenset({"GET"})),
        ("/events/{event_id}/reminders/confirmation", frozenset({"PUT"})),
    }


def test_event_reminder_confirmation_route_toggles_only_owned_future_reminder(
    db: Database,
) -> None:
    """Web 只能提前确认并重新开启当前用户日程中的指定未来提醒。"""

    owner_id    = "owner-web-reminder-toggle"
    event_id    = "future-web-reminder"
    remind_time = "2099-01-01T08:00:00+00:00"
    db.insert_item(
        {
            "id": event_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "未来提醒",
            "start_time": "2099-01-01T09:00:00+00:00",
            "remind_times": [remind_time],
        }
    )

    confirmed = events_api.set_event_reminder_confirmation(
        event_id,
        body=events_api.EventReminderConfirmationUpdate(
            remind_time = remind_time,
            confirmed   = True,
        ),
        owner_id = owner_id,
        db       = db,
    )
    assert confirmed["message"] == "提醒已提前确认"
    assert cast(dict[str, Any], confirmed["data"])["reminder"]["status"] == "confirmed"

    reopened = events_api.set_event_reminder_confirmation(
        event_id,
        body=events_api.EventReminderConfirmationUpdate(
            remind_time = remind_time,
            confirmed   = False,
        ),
        owner_id = owner_id,
        db       = db,
    )
    assert reopened["message"] == "提醒已重新开启"
    reminder = cast(dict[str, Any], reopened["data"])["reminder"]
    assert reminder["status"] == "pending"
    assert reminder["confirmed_at"] is None

    for request_owner, request_time in (
        ("another-owner", remind_time),
        (owner_id, "2099-01-01T07:00:00+00:00"),
    ):
        with pytest.raises(HTTPException) as missing:
            events_api.set_event_reminder_confirmation(
                event_id,
                body=events_api.EventReminderConfirmationUpdate(
                    remind_time = request_time,
                    confirmed   = True,
                ),
                owner_id = request_owner,
                db       = db,
            )
        assert missing.value.status_code == 404


def test_event_reminder_confirmation_route_rejects_expired_or_invalid_time(
    db: Database,
) -> None:
    """到期提醒和非规范时间不能通过 Web 改写确认状态。"""

    owner_id    = "owner-expired-web-reminder"
    event_id    = "expired-web-reminder"
    remind_time = "2000-01-01T08:00:00+00:00"
    db.insert_item(
        {
            "id": event_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "已到期提醒",
            "start_time": "2000-01-01T09:00:00+00:00",
            "remind_times": [remind_time],
        }
    )

    with pytest.raises(HTTPException) as expired:
        events_api.set_event_reminder_confirmation(
            event_id,
            body=events_api.EventReminderConfirmationUpdate(
                remind_time = remind_time,
                confirmed   = True,
            ),
            owner_id = owner_id,
            db       = db,
        )
    assert expired.value.status_code == 409

    with pytest.raises(HTTPException) as invalid:
        events_api.set_event_reminder_confirmation(
            event_id,
            body=events_api.EventReminderConfirmationUpdate(
                remind_time = "2000-01-01T08:00:00",
                confirmed   = True,
            ),
            owner_id = owner_id,
            db       = db,
        )
    assert invalid.value.status_code == 422


def test_collection_create_models_do_not_share_mutable_tag_defaults() -> None:
    """不同请求模型不得共享默认标签列表。"""

    first = events_api.EventCollectionCreate(
        title    = "第一组",
        children = [
            events_api.EventCollectionChildCreate(title="一", start_time="2030-01-01T10:00:00"),
            events_api.EventCollectionChildCreate(title="二", start_time="2030-01-02T10:00:00"),
        ],
    )
    second = events_api.EventCollectionCreate(
        title    = "第二组",
        children = [
            events_api.EventCollectionChildCreate(title="一", start_time="2030-01-01T10:00:00"),
            events_api.EventCollectionChildCreate(title="二", start_time="2030-01-02T10:00:00"),
        ],
    )

    first.tags.append("只属于第一组")

    assert second.tags == []


def test_category_endpoint_reads_scoped_categories_without_building_an_overview(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """分类下拉框只应扫描当前用户日程，不能构造超大日期概览。"""

    for item_id, owner_id, category in (
        ("category-a", "owner-a", "生活"),
        ("category-b", "owner-a", "工作"),
        ("category-blank", "owner-a", "待清空"),
        ("category-other", "owner-b", "其他用户"),
    ):
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "event",
                "title": item_id,
                "category": category,
                "start_time": "2030-01-01T10:00:00",
            }
        )
    # 模拟旧版本遗留的空分类；读接口仍应给出稳定的默认显示值。
    with db.get_connection():
        db.get_connection().execute(
            "UPDATE items SET category = '' WHERE id = ?",
            ("category-blank",),
        )

    def fail_if_overview_is_built(**_kwargs: object) -> dict[str, object]:
        """旧实现一旦回归到概览构造便立即失败。"""

        raise AssertionError("category endpoint must not build the event overview")

    monkeypatch.setattr(events_api, "build_events_overview", fail_if_overview_is_built)

    response = events_api.get_event_categories(owner_id="owner-a", db=db)

    assert response == {
        "ok": True,
        "data": {"categories": ["工作", "未分类", "生活"]},
        "message": "",
    }


def test_overview_endpoint_forwards_every_filter_and_wraps_the_result(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """概览端点只负责所有者范围转发和统一响应信封。"""

    overview = {"events": [{"id": "event-a"}], "summary": {"event_count": 1}}
    builder = Mock(return_value=overview)
    monkeypatch.setattr(events_api, "build_events_overview", builder)

    response = events_api.get_events_overview(
        start_date = "2030-01-01",
        end_date   = "2030-01-31",
        keyword    = "发布",
        category   = "项目",
        kind       = "multi_node",
        reminder   = "with",
        owner_id   = "overview-owner",
        db         = db,
    )

    assert response == {"ok": True, "data": overview, "message": ""}
    builder.assert_called_once_with(
        db         = db,
        owner_id   = "overview-owner",
        start_date = "2030-01-01",
        end_date   = "2030-01-31",
        keyword    = "发布",
        category   = "项目",
        kind       = "multi_node",
        reminder   = "with",
    )


def test_overview_endpoint_translates_invalid_query_to_422(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """分析层的日期与枚举校验错误应作为客户端错误返回。"""

    monkeypatch.setattr(
        events_api,
        "build_events_overview",
        Mock(side_effect=ValueError("unsupported reminder filter: later")),
    )

    with pytest.raises(HTTPException) as exc_info:
        events_api.get_events_overview(
            start_date = "2030-01-01",
            end_date   = "2030-01-31",
            reminder   = "later",
            owner_id   = "overview-owner",
            db         = db,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "unsupported reminder filter: later"


def test_create_collection_rejects_wrong_kind_and_too_few_children(db: Database) -> None:
    """创建入口应在写库前拒绝错误集合类型和单节点伪集合。"""

    valid_children = [
        events_api.EventCollectionChildCreate(title="一", start_time="2030-01-01T10:00:00"),
        events_api.EventCollectionChildCreate(title="二", start_time="2030-01-02T10:00:00"),
    ]
    with pytest.raises(HTTPException) as kind_error:
        events_api.create_event_collection(
            body=events_api.EventCollectionCreate(
                kind     = "recurring",
                title    = "错误类型",
                children = valid_children,
            ),
            owner_id = "owner-create-errors",
            db       = db,
        )
    assert kind_error.value.status_code == 422

    with pytest.raises(HTTPException) as child_error:
        events_api.create_event_collection(
            body=events_api.EventCollectionCreate(
                title    = "节点不足",
                children = valid_children[:1],
            ),
            owner_id = "owner-create-errors",
            db       = db,
        )
    assert child_error.value.status_code == 422


def test_create_collection_falls_back_when_clock_timezone_has_no_zone_key(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """自定义时钟只给固定偏移时，集合仍应使用配置中的合法默认时区。"""

    monkeypatch.setattr(
        events_api,
        "now_in_timezone",
        lambda _owner_id, _db: datetime(2030, 1, 1, tzinfo=UTC),
    )

    collection_id, _child_ids = _create_collection(db, "owner-clock-fallback")
    collection = db.get_event_collection(collection_id, "owner-clock-fallback")

    assert collection is not None
    assert collection["timezone"] == "Asia/Shanghai"


def test_create_collection_normalizes_shared_fields_and_orders_offset_times(
    db: Database,
) -> None:
    """集合头应复用叶子规范值，并按真实时刻而非字符串顺序计算边界。"""

    owner_id = "owner-create-normalized"
    db.update_user_settings(
        owner_id,
        {"timezone": "America/Los_Angeles", "default_category": "工作手稿"},
    )

    created = events_api.create_event_collection(
        body=events_api.EventCollectionCreate(
            title    = "\x00  跨区发布  ",
            content  = "\x01  正文  ",
            category = "未分类",
            location = " 线上 ",
            tags     = ["Alpha", "alpha", "二期"],
            notes    = "\x02  集合备注  ",
            children = [
                events_api.EventCollectionChildCreate(
                    title      = "早发生但日期字符串较大",
                    start_time = "2030-01-02T00:30:00+14:00",
                ),
                events_api.EventCollectionChildCreate(
                    title      = "晚发生但日期字符串较小",
                    start_time = "2030-01-01T23:00:00-10:00",
                ),
            ],
        ),
        owner_id = owner_id,
        db       = db,
    )
    collection_id = str(cast(dict[str, Any], created["data"])["id"])
    collection    = db.get_event_collection(collection_id, owner_id)

    assert collection is not None
    assert collection["title"] == "跨区发布"
    assert collection["content"] == "正文"
    assert collection["category"] == "工作手稿"
    assert collection["location"] == "线上"
    assert collection["tags"] == ["Alpha", "二期"]
    assert collection["notes"] == "集合备注"
    assert collection["timezone"] == "America/Los_Angeles"
    assert collection["start_time"] == "2030-01-01T10:30:00+00:00"
    assert collection["end_time"] == "2030-01-02T09:00:00+00:00"

    children = db.get_collection_events(collection_id, owner_id)
    assert [child.category for child in children] == ["工作手稿", "工作手稿"]
    assert [child.tags for child in children] == [["Alpha", "二期"], ["Alpha", "二期"]]
    assert [child.timezone for child in children] == [
        "America/Los_Angeles",
        "America/Los_Angeles",
    ]


def test_update_collection_normalizes_metadata_and_all_child_reminders(db: Database) -> None:
    """混合更新应一次写入规范化集合字段、所有叶子提醒和一条审计。"""

    owner_id = "owner-update-normalized"
    db.update_user_settings(
        owner_id,
        {"timezone": "America/New_York", "default_category": "默认工作"},
    )
    collection_id, child_ids = _create_collection(db, owner_id)

    response = events_api.update_collection(
        collection_id,
        body=events_api.EventCollectionUpdate(
            title          = "\x00  发布项目 v2  ",
            category       = "未分类",
            location       = None,
            tags           = ["Release", "release", "二期"],
            notes          = "\x01  新备注  ",
            timezone       = None,
            reminder_rules = [
                {"offset_seconds": 0},
                {"offset_seconds": 3600},
                {"offset_seconds": 3600},
            ],
        ),
        owner_id = owner_id,
        db       = db,
    )

    assert response["ok"] is True
    collection = db.get_event_collection(collection_id, owner_id)
    assert collection is not None
    assert collection["title"] == "发布项目 v2"
    assert collection["category"] == "默认工作"
    assert collection["location"] == ""
    assert collection["tags"] == ["Release", "二期"]
    assert collection["notes"] == "新备注"
    assert collection["timezone"] == "America/New_York"
    assert collection["reminder_rules"] == [
        {"offset_seconds": 3600},
        {"offset_seconds": 0},
    ]

    for child_id, expected_times in zip(
        child_ids,
        (
            ["2030-05-01T13:00:00+00:00", "2030-05-01T14:00:00+00:00"],
            ["2030-05-02T21:00:00+00:00", "2030-05-02T22:00:00+00:00"],
        ),
        strict=True,
    ):
        child = db.get_item(child_id, owner_id)
        assert child is not None
        assert child.reminder_rules == collection["reminder_rules"]
        assert child.remind_times == expected_times

    update_rows = _operation_rows(db, owner_id, "update_event_collection")
    assert len(update_rows) == 1
    assert update_rows[0]["item_id"] == collection_id
    logged_updates = json.loads(str(update_rows[0]["details"]))["updates"]
    assert logged_updates["title"] == "发布项目 v2"
    assert logged_updates["reminder_rules"] == collection["reminder_rules"]


def test_explicit_null_reminder_rules_clear_collection_and_children(db: Database) -> None:
    """显式 null 与空列表都表示清空提醒，不得只清集合头。"""

    owner_id = "owner-clear-reminders"
    collection_id, child_ids = _create_collection(
        db,
        owner_id,
        reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
    )

    events_api.update_collection(
        collection_id,
        body=events_api.EventCollectionUpdate(reminder_rules=None),
        owner_id = owner_id,
        db       = db,
    )

    collection = db.get_event_collection(collection_id, owner_id)
    assert collection is not None and collection["reminder_rules"] == []
    for child_id in child_ids:
        child = db.get_item(child_id, owner_id)
        assert child is not None
        assert child.reminder_rules == []
        assert child.remind_times == []
        assert db.get_reminder_logs(child_id) == []


def test_mixed_update_rolls_back_metadata_and_reminders_when_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """审计写入失败时，集合头和两个叶子提醒必须全部回滚。"""

    owner_id = "owner-update-rollback"
    collection_id, child_ids = _create_collection(db, owner_id, title="旧标题")
    original_log = db._log_operation_with_cursor

    def fail_update_audit(cursor: Any, **kwargs: Any) -> None:
        """仅在更新审计阶段注入失败，确保数据写入已经执行。"""

        if kwargs.get("action") == "update_event_collection":
            raise RuntimeError("注入更新审计失败")
        original_log(cursor, **kwargs)

    monkeypatch.setattr(db, "_log_operation_with_cursor", fail_update_audit)

    with pytest.raises(RuntimeError, match="注入更新审计失败"):
        events_api.update_collection(
            collection_id,
            body=events_api.EventCollectionUpdate(
                title          = "不应提交的新标题",
                reminder_rules = [{"offset_seconds": 3600}, {"offset_seconds": 0}],
            ),
            owner_id = owner_id,
            db       = db,
        )

    row = (
        db.get_connection()
        .execute(
            "SELECT title, reminder_rules FROM event_collections WHERE id = ?",
            (collection_id,),
        )
        .fetchone()
    )
    assert row is not None
    assert row["title"] == "旧标题"
    assert json.loads(row["reminder_rules"]) == [{"offset_seconds": 0}]
    for child_id in child_ids:
        child_row = (
            db.get_connection()
            .execute(
                "SELECT reminder_rules, remind_times FROM items WHERE id = ?",
                (child_id,),
            )
            .fetchone()
        )
        assert child_row is not None
        assert json.loads(child_row["reminder_rules"]) == [{"offset_seconds": 0}]
        assert len(json.loads(child_row["remind_times"])) == 1
    assert _operation_rows(db, owner_id, "update_event_collection") == []


def test_delete_collection_writes_one_atomic_audit_with_child_ids(db: Database) -> None:
    """成功删除应把集合、叶子和一条可撤销审计作为同一事务提交。"""

    owner_id = "owner-delete-success"
    collection_id, child_ids = _create_collection(db, owner_id)

    response = events_api.delete_collection(collection_id, owner_id=owner_id, db=db)

    assert response["ok"] is True
    assert db.get_event_collection(collection_id, owner_id) is None
    assert all(db.get_item(child_id, owner_id) is None for child_id in child_ids)
    delete_rows = _operation_rows(db, owner_id, "delete_event_collection")
    assert len(delete_rows) == 1
    assert json.loads(str(delete_rows[0]["details"])) == {"child_ids": child_ids}


def test_delete_collection_rolls_back_data_when_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """删除审计失败时不得留下已删除集合或叶子。"""

    owner_id = "owner-delete-rollback"
    collection_id, child_ids = _create_collection(db, owner_id)
    original_log = db._log_operation_with_cursor

    def fail_delete_audit(cursor: Any, **kwargs: Any) -> None:
        """在集合和叶子已标记删除后注入日志失败。"""

        if kwargs.get("action") == "delete_event_collection":
            raise RuntimeError("注入删除审计失败")
        original_log(cursor, **kwargs)

    monkeypatch.setattr(db, "_log_operation_with_cursor", fail_delete_audit)

    with pytest.raises(RuntimeError, match="注入删除审计失败"):
        events_api.delete_collection(collection_id, owner_id=owner_id, db=db)

    collection_row = (
        db.get_connection()
        .execute(
            "SELECT deleted FROM event_collections WHERE id = ?",
            (collection_id,),
        )
        .fetchone()
    )
    assert collection_row is not None and collection_row["deleted"] == 0
    child_rows = (
        db.get_connection()
        .execute(
            "SELECT id, deleted FROM items WHERE event_collection_id = ? ORDER BY id",
            (collection_id,),
        )
        .fetchall()
    )
    assert [row["id"] for row in child_rows] == sorted(child_ids)
    assert all(row["deleted"] == 0 for row in child_rows)
    assert _operation_rows(db, owner_id, "delete_event_collection") == []


def test_detail_endpoints_return_builder_data_and_translate_missing_records(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """集合和叶子详情端点都应保留统一信封及明确的 404。"""

    collection_detail = {"collection": {"id": "collection-a"}, "children": []}
    event_detail      = {"event": {"id": "event-a"}, "reminder_logs": []}
    collection_builder = Mock(side_effect=[collection_detail, None])
    event_builder = Mock(side_effect=[event_detail, None])
    monkeypatch.setattr(events_api, "build_event_collection_detail", collection_builder)
    monkeypatch.setattr(events_api, "build_event_detail", event_builder)

    assert events_api.get_collection_detail("collection-a", owner_id="owner", db=db) == {
        "ok": True,
        "data": collection_detail,
        "message": "",
    }
    with pytest.raises(HTTPException) as collection_error:
        events_api.get_collection_detail("missing", owner_id="owner", db=db)
    assert collection_error.value.status_code == 404

    assert events_api.get_event_detail("event-a", owner_id="owner", db=db) == {
        "ok": True,
        "data": event_detail,
        "message": "",
    }
    with pytest.raises(HTTPException) as event_error:
        events_api.get_event_detail("missing", owner_id="owner", db=db)
    assert event_error.value.status_code == 404


def test_update_collection_translates_repository_races(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """并发删除集合或叶子时应分别返回 404 与可重试的 409。"""

    owner_id = "owner-update-races"
    collection_id, _child_ids = _create_collection(db, owner_id)
    monkeypatch.setattr(db, "update_event_collection", lambda *_args, **_kwargs: False)

    with pytest.raises(HTTPException) as lost_header:
        events_api.update_collection(
            collection_id,
            body=events_api.EventCollectionUpdate(category="项目"),
            owner_id = owner_id,
            db       = db,
        )
    assert lost_header.value.status_code == 404

    def lose_collection(*_args: object, **_kwargs: object) -> int:
        """模拟读取后集合被另一请求删除。"""

        raise ItemNotFoundException(collection_id)

    monkeypatch.setattr(db, "update_event_collection_reminders", lose_collection)
    with pytest.raises(HTTPException) as missing_collection:
        events_api.update_collection(
            collection_id,
            body=events_api.EventCollectionUpdate(reminder_rules=[]),
            owner_id = owner_id,
            db       = db,
        )
    assert missing_collection.value.status_code == 404

    def lose_child(*_args: object, **_kwargs: object) -> int:
        """模拟集合图读取后其中一个叶子被并发删除。"""

        raise ItemNotFoundException("missing-child")

    monkeypatch.setattr(db, "update_event_collection_reminders", lose_child)
    with pytest.raises(HTTPException) as changed_collection:
        events_api.update_collection(
            collection_id,
            body=events_api.EventCollectionUpdate(reminder_rules=[]),
            owner_id = owner_id,
            db       = db,
        )
    assert changed_collection.value.status_code == 409
    assert changed_collection.value.detail == "Event collection changed; reload and retry"


def test_update_without_collection_start_uses_validation_anchor(db: Database) -> None:
    """旧集合缺少起始时间时，元数据更新不应被共享校验器误拒绝。"""

    owner_id      = "owner-no-collection-start"
    collection_id = "collection-without-start"
    db.create_event_collection(
        {
            "id": collection_id,
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "旧集合",
        }
    )

    response = events_api.update_collection(
        collection_id,
        body=events_api.EventCollectionUpdate(title="新集合"),
        owner_id = owner_id,
        db       = db,
    )

    assert response["ok"] is True
    collection = db.get_event_collection(collection_id, owner_id)
    assert collection is not None and collection["title"] == "新集合"


def test_update_collection_distinguishes_empty_request_from_missing_collection(
    db: Database,
) -> None:
    """空更新应返回 422，真实不存在的集合才返回 404。"""

    owner_id = "owner-update-errors"
    collection_id, _child_ids = _create_collection(db, owner_id)

    with pytest.raises(HTTPException) as empty_error:
        events_api.update_collection(
            collection_id,
            body     = events_api.EventCollectionUpdate(),
            owner_id = owner_id,
            db       = db,
        )
    assert empty_error.value.status_code == 422
    assert empty_error.value.detail == "No collection fields to update"

    with pytest.raises(HTTPException) as missing_error:
        events_api.update_collection(
            "missing-collection",
            body=events_api.EventCollectionUpdate(title="新标题"),
            owner_id = owner_id,
            db       = db,
        )
    assert missing_error.value.status_code == 404
    assert missing_error.value.detail == "Event collection not found"

    with pytest.raises(HTTPException) as missing_delete:
        events_api.delete_collection("missing-collection", owner_id=owner_id, db=db)
    assert missing_delete.value.status_code == 404
