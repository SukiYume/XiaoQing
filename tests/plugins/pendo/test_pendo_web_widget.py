from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.services.db import Database
from tests.helpers.paths import REPOSITORY_ROOT

try:
    from plugins.pendo.web.auth import generate_widget_token
except ModuleNotFoundError:
    pytest.skip("pendo web widget requires PyJWT", allow_module_level=True)
from plugins.pendo.web.api import widget as widget_api

ROOT = REPOSITORY_ROOT


def _seed_widget_data(db: Database, owner_id: str) -> None:
    """写入日程、待办、账目和笔记的小组件代表样本。"""

    for item in [
        {
            "id": "event_today",
            "owner_id": owner_id,
            "type": "event",
            "title": "今天会议",
            "start_time": "2026-03-25T10:00:00",
            "end_time": "2026-03-25T11:00:00",
            "location": "A1",
        },
        {
            "id": "event_tomorrow",
            "owner_id": owner_id,
            "type": "event",
            "title": "明天复盘",
            "start_time": "2026-03-26T14:00:00",
            "end_time": "2026-03-26T15:00:00",
            "location": "线上",
        },
        {
            "id": "event_later",
            "owner_id": owner_id,
            "type": "event",
            "title": "周末活动",
            "start_time": "2026-03-28T09:00:00",
            "end_time": "2026-03-28T10:30:00",
        },
        {
            "id": "task_focus",
            "owner_id": owner_id,
            "type": "task",
            "title": "处理周报",
            "status": "open",
            "priority": 1,
            "plan_date": "2026-03-25",
            "deadline_at": "2026-03-25T18:00:00",
            "created_at": "2026-03-24T08:00:00",
            "updated_at": "2026-03-25T08:30:00",
        },
        {
            "id": "task_next",
            "owner_id": owner_id,
            "type": "task",
            "title": "整理收据",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-03-27",
            "deadline_at": "2026-03-27T18:00:00",
            "created_at": "2026-03-24T08:00:00",
            "updated_at": "2026-03-24T08:00:00",
        },
        {
            "id": "ledger_expense",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午饭",
            "amount": 35.5,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-20",
        },
        {
            "id": "ledger_income",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "工资",
            "amount": 5000,
            "transaction_type": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-03-21",
        },
        {
            "id": "note_recent",
            "owner_id": owner_id,
            "type": "note",
            "title": "Radcliffe Wave 线索",
            "content": "整理近几天的观测想法和参考资料。",
            "category": "科研",
            "created_at": "2026-03-24T21:00:00",
            "updated_at": "2026-03-25T08:00:00",
            "tags": ["paper"],
        },
        {
            "id": "note_older",
            "owner_id": owner_id,
            "type": "note",
            "title": "SED 拆解",
            "content": "把滤光片组合和拟合过程记一下。",
            "category": "科研",
            "created_at": "2026-03-20T21:00:00",
            "updated_at": "2026-03-21T08:00:00",
        },
    ]:
        db.insert_item(item)


def _headers(token: str) -> dict[str, str]:
    """生成 Bearer 认证头。"""

    return {"Authorization": f"Bearer {token}"}


def test_widget_summary_returns_agenda_and_task_panel(client: Any, temp_db: Database) -> None:
    """任务板块同时返回三十天议程与聚焦待办。"""

    owner_id = "u-widget"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id, db=temp_db)

    res = client.get(
        "/api/widget/summary",
        params  = {"section": "tasks", "now": "2026-03-25T09:30:00"},
        headers = _headers(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True

    data = body["data"]
    assert data["section"] == "tasks"
    assert data["section_requested"] == "tasks"
    assert data["agenda"]["today_count"] == 1
    assert data["agenda"]["tomorrow_count"] == 1
    assert [item["title"] for item in data["agenda"]["items"]] == [
        "今天会议",
        "明天复盘",
        "周末活动",
    ]
    assert data["panel"]["title"] == "待办"
    assert data["panel"]["summary"]["primary"] == "2 项待办"
    assert "今日聚焦" in data["panel"]["summary"]["secondary"]
    assert [item["title"] for item in data["panel"]["items"][:2]] == ["处理周报", "整理收据"]
    assert data["links"]["events"] == "#/events"
    assert data["links"]["tasks"] == "#/tasks"


def test_widget_calendar_sync_returns_gap_and_full_unbounded_item_list(
    client: Any,
    temp_db: Database,
) -> None:
    """日历同步窗口包含两次运行间的历史缺口，且不受五条摘要上限约束。"""

    owner_id = "u-widget-calendar-gap"
    for index, day in enumerate(
        ["2026-08-01", "2026-08-03", "2026-08-05", "2026-08-07", "2026-08-09", "2026-08-11"],
        start=1,
    ):
        temp_db.insert_item(
            {
                "id": f"calendar-gap-{index}",
                "owner_id": owner_id,
                "type": "event",
                "title": f"补齐日程 {index}",
                "start_time": f"{day}T10:00:00",
                "end_time": f"{day}T11:00:00",
            }
        )
    temp_db.insert_item(
        {
            "id": "calendar-outside",
            "owner_id": owner_id,
            "type": "event",
            "title": "窗口外日程",
            "start_time": "2026-09-16T10:00:00",
        }
    )
    temp_db.insert_item(
        {
            "id": "calendar-other-owner",
            "owner_id": "u-widget-calendar-other",
            "type": "event",
            "title": "其他用户日程",
            "start_time": "2026-08-05T12:00:00",
        }
    )

    response = client.get(
        "/api/widget/calendar",
        params={"start_date": "2026-07-31", "end_date": "2026-09-14"},
        headers=_headers(generate_widget_token(owner_id, db=temp_db)),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["start_date"] == "2026-07-31"
    assert data["end_date"] == "2026-09-14"
    assert [item["title"] for item in data["items"]] == [
        f"补齐日程 {index}" for index in range(1, 7)
    ]
    assert [item["id"] for item in data["items"]] == [
        f"calendar-gap-{index}" for index in range(1, 7)
    ]


def test_widget_calendar_sync_uses_item_identity_instead_of_title_and_time(
    client: Any,
    temp_db: Database,
) -> None:
    """同名同刻条目全部返回，同一跨天条目只返回一次。"""

    owner_id = "u-widget-calendar-identity"
    for event_id, location in (("same-time-a", "A 楼"), ("same-time-b", "B 楼")):
        temp_db.insert_item(
            {
                "id": event_id,
                "owner_id": owner_id,
                "type": "event",
                "title": "同步会议",
                "start_time": "2026-08-10T10:00:00",
                "end_time": "2026-08-10T11:00:00",
                "location": location,
            }
        )
    temp_db.insert_item(
        {
            "id": "cross-day",
            "owner_id": owner_id,
            "type": "event",
            "title": "跨天出差",
            "start_time": "2026-08-11T10:00:00",
            "end_time": "2026-08-13T18:00:00",
        }
    )

    response = client.get(
        "/api/widget/calendar",
        params={"start_date": "2026-08-10", "end_date": "2026-08-13"},
        headers=_headers(generate_widget_token(owner_id, db=temp_db)),
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [(item["id"], item["location"]) for item in items] == [
        ("same-time-a", "A 楼"),
        ("same-time-b", "B 楼"),
        ("cross-day", ""),
    ]


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("not-a-date", "2026-09-14"),
        ("2026-07-31", "2026-02-30"),
        ("2026-09-14", "2026-07-31"),
        ("0001-01-01", "9999-12-31"),
    ],
)
def test_widget_calendar_sync_rejects_invalid_ranges(
    client: Any,
    temp_db: Database,
    start_date: str,
    end_date: str,
) -> None:
    response = client.get(
        "/api/widget/calendar",
        params={"start_date": start_date, "end_date": end_date},
        headers=_headers(generate_widget_token("u-widget-calendar-invalid", db=temp_db)),
    )

    assert response.status_code == 422


def test_build_widget_summary_returns_expected_panels_directly(temp_db: Database) -> None:
    """直接调用真实模块时仍返回与 HTTP 路由一致的板块结构。"""

    owner_id = "u-widget-direct"
    _seed_widget_data(temp_db, owner_id)

    tasks_data = widget_api.build_widget_summary(
        temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00"
    )
    ledger_data = widget_api.build_widget_summary(
        temp_db, owner_id, section="ledger", now="2026-03-25T09:30:00"
    )

    assert tasks_data["section"] == "tasks"
    assert tasks_data["agenda"]["today_count"] == 1
    assert tasks_data["panel"]["items"][0]["title"] == "处理周报"
    assert ledger_data["section"] == "ledger"
    assert ledger_data["panel"]["items"][0]["amount_text"] == "-¥36"


def test_parse_now_uses_user_timezone_and_converts_aware_inputs(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺省与带偏移时间都转换到当前用户时区，而非固定默认时区。"""

    owner_id = "u-widget-timezone"
    temp_db.update_user_settings(owner_id, {"timezone": "America/Los_Angeles"})
    monkeypatch.setattr(
        widget_api,
        "now_in_timezone",
        lambda requested_owner, requested_db: datetime.fromisoformat(
            "2026-03-25T09:30:12.345678-07:00"
        ),
    )

    assert widget_api._parse_now(None, owner_id, temp_db) == datetime(2026, 3, 25, 9, 30, 12)
    assert widget_api._parse_now("2026-03-25T16:30:00+00:00", owner_id, temp_db) == datetime(
        2026, 3, 25, 9, 30
    )
    with pytest.raises(widget_api.HTTPException) as exc_info:
        widget_api._parse_now("not-a-time", owner_id, temp_db)
    assert exc_info.value.status_code == 422


def test_widget_text_and_meta_helpers_cover_compact_boundaries() -> None:
    """标题、正文和元信息都遵守紧凑展示边界。"""

    assert widget_api._title_text("   ") == "无标题"
    assert widget_api._title_text("abcdef", limit=5) == "abcd…"
    assert widget_api._preview_text("  one\n two  ") == "one two"
    assert widget_api._preview_text("abcdef", limit=5) == "abcd…"
    assert widget_api._format_event_meta({"location": "A1"}) == "A1"
    user_timezone = ZoneInfo("Asia/Shanghai")
    assert (
        widget_api._format_task_meta({"status": "cancelled"}, "2026-03-25", user_timezone)
        == "已取消"
    )
    assert (
        widget_api._format_task_meta(
            {"status": "open", "deadline_at": "2026-03-25T23:00:00+08:00"},
            "2026-03-25",
            user_timezone,
        )
        == "待办 · 今天"
    )
    assert (
        widget_api._format_task_meta(
            {"status": "open", "deadline_at": "not-an-iso-time"},
            "2026-03-25",
            user_timezone,
        )
        == "待办"
    )


def test_widget_all_section_includes_every_panel_and_overdue_summary(
    temp_db: Database,
) -> None:
    """全量模式一次返回三个板块，并在待办副摘要中显示逾期数。"""

    owner_id = "u-widget-all"
    _seed_widget_data(temp_db, owner_id)
    temp_db.insert_item(
        {
            "id": "task-overdue",
            "owner_id": owner_id,
            "type": "task",
            "title": "逾期任务",
            "status": "open",
            "plan_date": "2026-03-24",
        }
    )

    data = widget_api.build_widget_summary(
        temp_db, owner_id, section="all", now="2026-03-25T09:30:00"
    )

    assert data["section"] == "all"
    assert list(data["panels"]) == ["tasks", "ledger", "notes"]
    assert "1 项逾期" in data["panels"]["tasks"]["summary"]["secondary"]


@pytest.mark.parametrize("section", ["tasks", "notes"])
def test_widget_task_and_note_panels_do_not_materialize_full_item_sets(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    owner_id = f"u-widget-bounded-{section}"
    _seed_widget_data(temp_db, owner_id)
    monkeypatch.setattr(
        temp_db,
        "get_all_items",
        lambda *_args, **_kwargs: pytest.fail("widget must not load a full item set"),
    )
    monkeypatch.setattr(
        temp_db,
        "get_items",
        lambda *_args, **_kwargs: pytest.fail("task/note widget panels use bounded SQL"),
    )

    data = widget_api.build_widget_summary(
        temp_db,
        owner_id,
        section = section,
        now     = "2026-03-25T09:30:00",
    )

    assert data["panel"]["section"] == section


def test_build_widget_summary_batches_collection_titles_for_agenda(
    temp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多个集合日程只批量读取一次集合头，并把集合标题写入议程。"""

    owner_id = "u-widget-event-collection"
    for collection_id, collection_title, event_title, start_time in [
        ("widget-conf", "FRB2026会议", "摘要截止", "2026-03-25T10:00:00"),
        ("widget-observe", "春季观测", "开机检查", "2026-03-25T11:00:00"),
    ]:
        temp_db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": collection_title,
                "category": "学术",
                "start_time": start_time,
                "end_time": "2026-03-26T10:00:00",
            }
        )
        temp_db.insert_item(
            {
                "id": f"{collection_id}_m01",
                "owner_id": owner_id,
                "type": "event",
                "title": event_title,
                "category": "学术",
                "start_time": start_time,
                "event_role": "multi_node_child",
                "event_collection_id": collection_id,
                "event_collection_kind": "multi_node",
                "event_index": 1,
                "event_node_key": "m01",
            }
        )

    real_batch_lookup                  = temp_db.get_event_collections_by_ids
    calls: list[tuple[str, list[str]]] = []

    def track_batch_lookup(
        lookup_owner_id: str, collection_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        calls.append((lookup_owner_id, collection_ids))
        return real_batch_lookup(lookup_owner_id, collection_ids)

    monkeypatch.setattr(temp_db, "get_event_collections_by_ids", track_batch_lookup)

    data = widget_api.build_widget_summary(
        temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00"
    )

    assert calls == [(owner_id, ["widget-conf", "widget-observe"])]
    assert [item["title"] for item in data["agenda"]["items"]] == [
        "FRB2026会议 · 摘要截止",
        "春季观测 · 开机检查",
    ]


def test_widget_summary_supports_ledger_notes_and_auto_sections(
    client: Any, temp_db: Database
) -> None:
    """财务、笔记和按小时轮换的自动板块都保持公开响应形状。"""

    owner_id = "u-widget-sections"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id, db=temp_db)

    ledger_res = client.get(
        "/api/widget/summary",
        params  = {"section": "ledger", "now": "2026-03-25T09:30:00"},
        headers = _headers(token),
    )
    notes_res = client.get(
        "/api/widget/summary",
        params  = {"section": "notes", "now": "2026-03-25T11:30:00"},
        headers = _headers(token),
    )
    auto_res = client.get(
        "/api/widget/summary",
        params  = {"section": "auto", "now": "2026-03-25T11:30:00"},
        headers = _headers(token),
    )

    ledger_data = ledger_res.json()["data"]
    notes_data  = notes_res.json()["data"]
    auto_data   = auto_res.json()["data"]

    assert ledger_data["section"] == "ledger"
    assert ledger_data["panel"]["title"] == "财务"
    assert ledger_data["panel"]["items"][0]["title"] == "午饭"
    assert ledger_data["panel"]["items"][0]["amount_text"] == "-¥36"

    assert notes_data["section"] == "notes"
    assert notes_data["panel"]["title"] == "笔记"
    assert notes_data["panel"]["items"][0]["title"] == "Radcliffe Wave 线索"
    assert "观测想法" in notes_data["panel"]["items"][0]["preview"]

    assert auto_data["section_requested"] == "auto"
    assert auto_data["section"] == "notes"
    assert auto_data["panel"]["title"] == "笔记"


@pytest.mark.parametrize(
    "params",
    [
        {"section": "unknown"},
        {"section": "x" * 17},
        {"now": "not-a-time"},
        {"now": "2" * 65},
    ],
)
def test_widget_endpoint_rejects_invalid_or_oversized_query_values(
    client: Any,
    temp_db: Database,
    params: dict[str, str],
) -> None:
    """板块和时间查询在数据库工作前以 422 关闭失败。"""

    response = client.get(
        "/api/widget/summary",
        params=params,
        headers=_headers(generate_widget_token("u-widget-invalid-query", db=temp_db)),
    )

    assert response.status_code == 422


def test_widget_ledger_panel_marks_transfer_transactions(temp_db: Database) -> None:
    """转账条目使用中性箭头，不伪装成收入或支出。"""

    owner_id = "u-widget-transfer"
    _seed_widget_data(temp_db, owner_id)
    temp_db.insert_item(
        {
            "id": "ledger_transfer",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "转到储蓄卡",
            "amount": 1200,
            "transaction_type": "transfer",
            "ledger_category": "转账",
            "ledger_date": "2026-03-22",
            "account_name": "现金",
            "counter_account_name": "储蓄卡",
        }
    )

    data = widget_api.build_widget_summary(
        temp_db, owner_id, section="ledger", now="2026-03-25T09:30:00"
    )
    transfer = next(item for item in data["panel"]["items"] if item["title"] == "转到储蓄卡")

    assert transfer["transaction_type"] == "transfer"
    assert transfer["amount_text"] == "↔ ¥1200"


def test_build_widget_summary_auto_rotates_by_hour(temp_db: Database) -> None:
    """自动板块按任务、财务、笔记三小时周期稳定轮换。"""

    owner_id = "u-widget-auto"
    _seed_widget_data(temp_db, owner_id)

    assert (
        widget_api.build_widget_summary(
            temp_db, owner_id, section="auto", now="2026-03-25T09:30:00"
        )["section"]
        == "tasks"
    )
    assert (
        widget_api.build_widget_summary(
            temp_db, owner_id, section="auto", now="2026-03-25T10:30:00"
        )["section"]
        == "ledger"
    )
    assert (
        widget_api.build_widget_summary(
            temp_db, owner_id, section="auto", now="2026-03-25T11:30:00"
        )["section"]
        == "notes"
    )


def test_build_widget_summary_limits_agenda_to_five_items_within_thirty_days(
    temp_db: Database,
) -> None:
    """议程最多五项，且不读取三十天窗口外事件。"""

    owner_id = "u-widget-agenda-30d"
    _seed_widget_data(temp_db, owner_id)

    for index, start in enumerate(
        [
            "2026-03-29T09:00:00",
            "2026-04-03T09:00:00",
            "2026-04-10T09:00:00",
            "2026-04-20T09:00:00",
        ],
        start=1,
    ):
        temp_db.insert_item(
            {
                "id": f"event_extra_{index}",
                "owner_id": owner_id,
                "type": "event",
                "title": f"额外安排 {index}",
                "start_time": start,
                "end_time": start.replace("09:00:00", "10:00:00"),
            }
        )

    temp_db.insert_item(
        {
            "id": "event_outside_30d",
            "owner_id": owner_id,
            "type": "event",
            "title": "三十天外安排",
            "start_time": "2026-04-26T09:00:00",
            "end_time": "2026-04-26T10:00:00",
        }
    )

    data = widget_api.build_widget_summary(
        temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00"
    )
    titles = [item["title"] for item in data["agenda"]["items"]]

    assert len(titles) == 5
    assert titles == ["今天会议", "明天复盘", "周末活动", "额外安排 1", "额外安排 2"]
    assert "三十天外安排" not in titles


def test_build_widget_summary_limits_notes_panel_to_five_items(
    temp_db: Database,
) -> None:
    """笔记板块只保留最近五条并保持更新时间顺序。"""

    owner_id = "u-widget-notes-5"
    _seed_widget_data(temp_db, owner_id)

    for index, created_at in enumerate(
        [
            "2026-03-25T09:00:00",
            "2026-03-24T20:00:00",
            "2026-03-24T10:00:00",
            "2026-03-23T20:00:00",
        ],
        start=1,
    ):
        temp_db.insert_item(
            {
                "id": f"note_extra_{index}",
                "owner_id": owner_id,
                "type": "note",
                "title": f"额外笔记 {index}",
                "content": f"这是第 {index} 条额外笔记。",
                "category": "测试",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    data = widget_api.build_widget_summary(
        temp_db, owner_id, section="notes", now="2026-03-25T09:30:00"
    )
    titles = [item["title"] for item in data["panel"]["items"]]

    assert len(titles) == 5
    assert titles == ["额外笔记 1", "Radcliffe Wave 线索", "额外笔记 2", "额外笔记 3", "额外笔记 4"]
    assert "SED 拆解" not in titles


def test_widget_token_is_limited_to_widget_endpoint(client: Any, temp_db: Database) -> None:
    """小组件 token 只能访问小组件端点。"""

    owner_id = "u-widget-locked"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id, db=temp_db)

    widget_res = client.get(
        "/api/widget/summary",
        params  = {"section": "tasks", "now": "2026-03-25T09:30:00"},
        headers = _headers(token),
    )
    dashboard_res = client.get("/api/dashboard", headers=_headers(token))

    assert widget_res.status_code == 200
    assert dashboard_res.status_code == 403
    assert "Widget token" in dashboard_res.json()["message"]


def test_revoked_widget_token_is_rejected_by_http_dependency(
    client: Any,
    temp_db: Database,
) -> None:
    token = generate_widget_token("u-widget-revoked", db=temp_db)
    assert temp_db.revoke_widget_tokens("u-widget-revoked") == 1

    response = client.get(
        "/api/widget/summary",
        headers=_headers(token),
    )

    assert response.status_code == 401
    assert "revoked" in response.json()["message"]


def test_widget_endpoint_rejects_missing_auth(client: Any) -> None:
    """未认证请求不能读取小组件数据。"""

    res = client.get("/api/widget/summary")

    assert res.status_code == 401
    assert "Missing web session" in res.json()["message"]


def test_web_handler_never_inlines_widget_token_when_private_delivery_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法私聊投递凭据时，群消息错误不得内联敏感 token。"""

    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "plugins.pendo.web.server",
        types.SimpleNamespace(
            get_url    = lambda: "http://127.0.0.1:8765",
            is_running = lambda: True,
            start      = lambda _db: True,
            stop       = lambda: True,
        ),
    )

    web_module = importlib.import_module("plugins.pendo.handlers.web")

    monkeypatch.setattr(
        web_module, "generate_widget_token", lambda *_args, **_kwargs: "mock-widget-token"
    )

    handler = web_module.WebHandler(db=None)
    result = asyncio.run(handler.handle("1001", "widget token", context=None))

    assert result["status"] == "error"
    assert "无法通过私聊安全发送凭据" in result["message"]
    assert "mock-widget-token" not in result["message"]
