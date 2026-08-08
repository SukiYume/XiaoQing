"""Pendo Web 看板聚合器的跨模块回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.dashboard_overview import build_dashboard_overview


def _insert(db: Database, owner_id: str, **values: Any) -> None:
    """为当前所有者写入一条测试条目。"""

    db.insert_item({"owner_id": owner_id, **values})


def test_build_dashboard_overview_uses_month_events_and_mixed_task_buckets(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """看板应按一次覆盖查询构建月日程、未来议程和跨模块摘要。"""

    owner_id = "u-dashboard"
    _insert(
        db,
        owner_id,
        id="ev1",
        type="event",
        title="月内会议",
        start_time="2026-03-10T09:00:00",
        end_time="2026-03-10T10:00:00",
    )
    _insert(
        db,
        owner_id,
        id="ev2",
        type="event",
        title="月末复盘",
        start_time="2026-03-28T18:00:00",
    )
    _insert(
        db,
        owner_id,
        id="ev3",
        type="event",
        title="下月活动",
        start_time="2026-04-02T18:00:00",
    )
    db.create_event_collection(
        {
            "id": "ev4col",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "FRB2026会议",
            "content": "",
            "category": "学术",
            "location": "",
            "start_time": "2026-02-20T00:00:00",
            "end_time": "2026-04-02T12:00:00",
        }
    )
    for item_id, title, start_time, index in (
        ("ev4col_m01", "摘要截止", "2026-03-05T09:00:00", 1),
        ("ev4col_m02", "会议开始", "2026-04-01T10:00:00", 2),
    ):
        _insert(
            db,
            owner_id,
            id=item_id,
            type="event",
            title=title,
            category="学术",
            start_time=start_time,
            event_role="multi_node_child",
            event_collection_id="ev4col",
            event_collection_kind="multi_node",
            event_index=index,
            event_node_key=f"m0{index}",
        )

    _insert(
        db,
        owner_id,
        id="task1",
        type="task",
        title="未完成任务",
        status="open",
        priority=2,
        plan_date="2026-03-26",
        deadline_at="2026-03-26T10:00:00",
    )
    _insert(
        db,
        owner_id,
        id="task2",
        type="task",
        title="今日任务",
        status="open",
        priority=1,
        plan_date="2026-03-25",
        deadline_at="2026-03-25T18:00:00",
    )
    _insert(
        db,
        owner_id,
        id="task3",
        type="task",
        title="已完成任务",
        status="done",
        priority=3,
        completed_at="2026-03-24T21:00:00",
        updated_at="2026-03-24T21:00:00",
    )
    _insert(
        db,
        owner_id,
        id="ledger1",
        type="ledger",
        title="午饭",
        amount=25.5,
        transaction_type="expense",
        ledger_category="餐饮",
        ledger_date="2026-03-20",
    )
    _insert(
        db,
        owner_id,
        id="ledger2",
        type="ledger",
        title="工资",
        amount=3000,
        transaction_type="income",
        ledger_category="工资",
        ledger_date="2026-03-21",
    )
    _insert(
        db,
        owner_id,
        id="diary1",
        type="diary",
        title="三月日记",
        content="记录一下",
        diary_date="2026-03-05",
    )

    range_calls: list[tuple[str, str, str]] = []
    collection_batches: list[list[str]] = []
    get_events_for_range = db.get_events_for_range
    get_event_collections_by_ids = db.get_event_collections_by_ids

    def track_range(user_id: str, start: str, end: str) -> list[Any]:
        range_calls.append((user_id, start, end))
        return get_events_for_range(user_id, start, end)

    def track_collections(user_id: str, collection_ids: list[str]) -> dict[str, dict[str, Any]]:
        collection_batches.append(list(collection_ids))
        return get_event_collections_by_ids(user_id, collection_ids)

    monkeypatch.setattr(db, "get_events_for_range", track_range)
    monkeypatch.setattr(db, "get_event_collections_by_ids", track_collections)
    monkeypatch.setattr(
        db,
        "get_event_collection",
        lambda *_args, **_kwargs: pytest.fail("看板不应逐个读取日程集合"),
    )

    result = build_dashboard_overview(
        db=db,
        owner_id=owner_id,
        now=datetime(2026, 3, 25, 9, 30),
    )

    assert range_calls == [(owner_id, "2026-03-01T00:00:00", "2026-04-15T23:59:59")]
    assert collection_batches == [["ev4col"]]
    assert result["summary"] == {
        "events_month": 3,
        "tasks_pending": 2,
        "tasks_done_recent": 1,
        "ledger_month_expense": 25.5,
        "diary_month": 1,
    }
    assert len(result["events_month"]) == 3
    assert result["events_month"][0]["title"] == "FRB2026会议"
    assert result["events_month"][0]["display_subtitle"] == "摘要截止"
    assert result["events_month"][0]["start_time"] == "2026-03-05T09:00:00"
    assert all(event["start_time"][:10] <= "2026-03-31" for event in result["events_month"])
    assert any(
        event["title"] == "FRB2026会议"
        and event["display_subtitle"] == "会议开始"
        and event["start_time"] == "2026-04-01T10:00:00"
        for event in result["events_agenda"]
    )
    assert [task["title"] for task in result["tasks"]["active"]] == [
        "今日任务",
        "未完成任务",
    ]
    assert [task["title"] for task in result["tasks"]["completed"]] == ["已完成任务"]
    assert result["month_summary"] == {"income": 3000.0, "expense": 25.5, "balance": 2974.5}
    assert len(result["recent_ledger"]) == 2
    assert result["spending_trend"][0] == {"date": "2026-03-01", "amount": 0.0}
    assert result["spending_trend"][-1] == {"date": "2026-03-25", "amount": 0.0}
    assert {"date": "2026-03-20", "amount": 25.5} in result["spending_trend"]


def test_dashboard_overview_prefers_amount_cents_and_paginates_month_ledger(
    db: Database,
) -> None:
    """月度账本必须跨批次汇总，并以整数分字段为金额真值。"""

    owner_id = "u-dashboard-amounts"
    for index in range(505):
        _insert(
            db,
            owner_id,
            id=f"ledger_expense_{index}",
            type="ledger",
            title=f"批量支出 {index}",
            amount=0,
            amount_cents=100,
            transaction_type="expense",
            ledger_category="压力测试",
            ledger_date="2026-03-20",
        )
    _insert(
        db,
        owner_id,
        id="ledger_income_cents",
        type="ledger",
        title="收入",
        amount=0,
        amount_cents=12345,
        transaction_type="income",
        ledger_category="工资",
        ledger_date="2026-03-21",
    )

    result = build_dashboard_overview(
        db=db,
        owner_id=owner_id,
        now=datetime(2026, 3, 25, 9, 30),
    )

    assert result["summary"]["ledger_month_expense"] == 505.0
    assert result["month_summary"] == {"income": 123.45, "expense": 505.0, "balance": -381.55}
    assert {"date": "2026-03-20", "amount": 505.0} in result["spending_trend"]


def test_dashboard_active_task_sort_is_not_truncated_before_priority(
    db: Database,
) -> None:
    """排序前必须读取全部活动任务，不能漏掉首批之后的高优先级项。"""

    owner_id = "u-dashboard-task-order"
    for index in range(21):
        _insert(
            db,
            owner_id,
            id=f"ordinary-{index}",
            type="task",
            title=f"普通任务 {index}",
            status="open",
            priority=5,
            plan_date=f"2026-03-{index + 1:02d}",
        )
    _insert(
        db,
        owner_id,
        id="important-late-plan",
        type="task",
        title="高优先级远期任务",
        status="open",
        priority=1,
        plan_date="2026-12-31",
    )

    result = build_dashboard_overview(
        db=db,
        owner_id=owner_id,
        now=datetime(2026, 3, 25, 9, 30),
    )

    assert result["summary"]["tasks_pending"] == 22
    assert result["tasks"]["active"][0]["id"] == "important-late-plan"


def test_dashboard_converts_aware_now_to_user_wall_clock(db: Database) -> None:
    """带时区当前时间必须先转换到用户时区，再决定自然月边界。"""

    result = build_dashboard_overview(
        db=db,
        owner_id="u-dashboard-timezone",
        now=datetime(2026, 3, 31, 18, tzinfo=UTC),
    )

    assert result["spending_trend"] == [{"date": "2026-04-01", "amount": 0.0}]
