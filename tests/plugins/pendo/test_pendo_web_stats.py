"""Pendo Web 统计接口的聚合、校验与前端契约回归。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.pendo.services.db import Database
from plugins.pendo.web.api import stats as stats_api
from tests.helpers.assertions import assert_http_error as _assert_http_error
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_stats_router_exposes_only_documented_get_endpoints() -> None:
    """统计模块只注册九个只读公开入口。"""

    registered = {
        (route.path, frozenset(getattr(route, "methods", set())))
        for route in stats_api.router.routes
    }
    assert registered == {
        ("/stats/ledger", frozenset({"GET"})),
        ("/stats/ledger/insights", frozenset({"GET"})),
        ("/stats/tasks", frozenset({"GET"})),
        ("/stats/tasks/overview", frozenset({"GET"})),
        ("/stats/notes/overview", frozenset({"GET"})),
        ("/stats/diary/overview", frozenset({"GET"})),
        ("/stats/events", frozenset({"GET"})),
        ("/stats/ledger/comparison", frozenset({"GET"})),
        ("/stats/activity-heatmap", frozenset({"GET"})),
    }


def test_stats_http_preserves_range_alias_and_declared_query_bounds(db: Database) -> None:
    """真实 HTTP 层继续接收 ``range``，并在进入路由前拦截数值越界。"""

    owner_id = "u-stats-http"
    app = FastAPI()
    app.include_router(stats_api.router)
    app.dependency_overrides[stats_api.get_current_user] = lambda: owner_id
    app.dependency_overrides[stats_api.get_db] = lambda: db

    with TestClient(app) as client:
        response = client.get(
            "/stats/ledger",
            params={"range": "2026-03-01..2026-03-31"},
        )
        invalid_range = client.get("/stats/events", params={"range": "not-a-range"})
        invalid_months = client.get("/stats/ledger/comparison", params={"months": 2})
        invalid_year = client.get("/stats/activity-heatmap", params={"year": 1969})

    assert response.status_code == 200
    assert response.json()["data"]["monthly"] == []
    assert invalid_range.status_code == 422
    assert invalid_months.status_code == 422
    assert invalid_year.status_code == 422


def test_ledger_insights_normalizes_filters_and_validates_bounds(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本洞察清理文本，并拒绝不完整日期和反向金额区间。"""

    captured: dict[str, Any] = {}

    def fake_build_ledger_insights(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"marker": "ok"}

    monkeypatch.setattr(stats_api, "build_ledger_insights", fake_build_ledger_insights)
    result = stats_api.ledger_visual_insights(
        transaction_type="expense",
        category=" 餐饮 ",
        account_name=" 现金 ",
        start_date="2026-03-01",
        end_date="2026-03-31",
        amount_min=10,
        amount_max=20,
        compare_mode="previous_year_to_date",
        owner_id="u-insights",
        db=db,
    )

    assert result["data"] == {"marker": "ok"}
    assert captured["category"] == "餐饮"
    assert captured["account_name"] == "现金"
    assert captured["start_date"] == "2026-03-01"
    assert captured["compare_mode"] == "previous_year_to_date"
    _assert_http_error(
        422,
        lambda: stats_api.ledger_visual_insights(
            start_date="2026-03-01",
            owner_id="u-insights",
            db=db,
        ),
    )
    _assert_http_error(
        422,
        lambda: stats_api.ledger_visual_insights(
            start_date="2026-03-31",
            end_date="2026-03-01",
            owner_id="u-insights",
            db=db,
        ),
    )
    _assert_http_error(
        422,
        lambda: stats_api.ledger_visual_insights(
            amount_min=20,
            amount_max=10,
            owner_id="u-insights",
            db=db,
        ),
    )


def test_task_overview_wraps_analytics_payload(db: Database) -> None:
    """任务概览路由保持统一的 ok/data/message 响应外壳。"""

    result = stats_api.task_overview(today="2026-03-01", owner_id="u-task-overview", db=db)
    assert result["ok"] is True
    assert isinstance(result["data"], dict)
    assert result["message"] == ""


def test_task_overview_translates_builder_validation_error(db: Database) -> None:
    """任务分析器的非法 today 应转换为稳定的 HTTP 400。"""

    error = _assert_http_error(
        400,
        lambda: stats_api.task_overview(
            today="not-a-date",
            owner_id="u-task-invalid",
            db=db,
        ),
    )
    assert "valid ISO date" in error.detail


def test_ledger_stats_returns_expense_amount_histogram(db: Database) -> None:
    """支出直方图只统计支出，并正确覆盖首尾金额桶。"""

    owner_id = "u-ledger-stats"
    for title, amount, transaction_type, category, ledger_date in (
        ("早餐", 12, "expense", "餐饮", "2026-03-10"),
        ("打车", 48, "expense", "交通", "2026-03-11"),
        ("房租", 1200, "expense", "居住", "2026-03-12"),
        ("工资", 5000, "income", "工资", "2026-03-15"),
    ):
        db.insert_item(
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": title,
                "amount": amount,
                "transaction_type": transaction_type,
                "ledger_category": category,
                "ledger_date": ledger_date,
            }
        )

    result = stats_api.ledger_stats(
        range_str="2026-03-01..2026-03-31",
        owner_id=owner_id,
        db=db,
    )
    histogram = {
        item["bucket"]: item["count"] for item in result["data"]["expense_amount_histogram"]
    }

    assert histogram["0-20"] == 1
    assert histogram["20-50"] == 1
    assert histogram["1000+"] == 1
    assert sum(histogram.values()) == 3


def test_ledger_stats_respects_range_for_totals_categories_and_trend_data(
    db: Database,
) -> None:
    """账本趋势、分类和直方图必须共用同一个日期范围。"""

    owner_id = "u-ledger-range"
    for item in (
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "早餐",
            "amount": 18,
            "transaction_type": "expense",
            "ledger_category": " 餐饮 ",
            "ledger_date": "2026-03-02",
        },
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "兼职",
            "amount": 300,
            "transaction_type": "income",
            "ledger_category": "副业",
            "ledger_date": "2026-03-03",
        },
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "转账",
            "amount": 50,
            "transaction_type": "transfer",
            "ledger_category": "",
            "account_name": "现金",
            "counter_account_name": "银行卡",
            "ledger_date": "2026-03-04",
        },
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "遗留类型",
            "amount": 5,
            "transaction_type": "expense",
            "ledger_category": "遗留",
            "ledger_date": "2026-03-05",
        },
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "范围外支出",
            "amount": 66,
            "transaction_type": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-04-03",
        },
    ):
        db.insert_item(item)

    # 模拟升级前遗留的空白分类，确认统计层不会把空白当作独立类别。
    db.get_connection().execute(
        "UPDATE items SET ledger_category=' ' WHERE owner_id=? AND title='转账'",
        (owner_id,),
    )
    db.get_connection().execute(
        "UPDATE items SET transaction_type='legacy' WHERE owner_id=? AND title='遗留类型'",
        (owner_id,),
    )
    db.get_connection().commit()

    data = stats_api.ledger_stats(
        range_str="2026-03-01..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert data["monthly"] == [{"month": "2026-03", "income": 300, "expense": 18}]
    assert data["daily"] == [
        {"date": "2026-03-02", "income": 0, "expense": 18},
        {"date": "2026-03-03", "income": 300, "expense": 0},
        {"date": "2026-03-04", "income": 0, "expense": 0},
        {"date": "2026-03-05", "income": 0, "expense": 0},
    ]
    assert data["expense_by_category"] == [{"category": "餐饮", "total": 18}]
    assert data["income_by_category"] == [{"category": "副业", "total": 300}]
    assert data["transfer_by_category"] == [{"category": "未分类", "total": 50}]
    histogram = {item["bucket"]: item["count"] for item in data["expense_amount_histogram"]}
    assert histogram["0-20"] == 1
    assert sum(histogram.values()) == 1


def test_ledger_stats_all_range_stops_at_today_and_excludes_future_entries(
    db: Database,
) -> None:
    """“全部”范围以今天为上界，不能把未来流水算作已发生。"""

    owner_id = "u-ledger-all"
    now = datetime.now()
    included_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    future_date = (now + timedelta(days=30)).strftime("%Y-%m-%d")

    for title, amount, category, ledger_date in (
        ("已发生支出", 28, "餐饮", included_date),
        ("未来支出", 88, "未来", future_date),
    ):
        db.insert_item(
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": title,
                "amount": amount,
                "transaction_type": "expense",
                "ledger_category": category,
                "ledger_date": ledger_date,
            }
        )

    data = stats_api.ledger_stats(range_str="all", owner_id=owner_id, db=db)["data"]

    assert data["expense_by_category"] == [{"category": "餐饮", "total": 28}]
    assert all(item["date"] != future_date for item in data["daily"])
    assert sum(item["count"] for item in data["expense_amount_histogram"]) == 1


def test_event_stats_filters_range_and_builds_weekday_slot_matrix(db: Database) -> None:
    """日程周趋势、时段和星期矩阵必须排除范围外事件。"""

    owner_id = "u-event-stats"
    for title, category, start_time, end_time in (
        ("下午会议", " 工作 ", "2026-03-31T14:00:00", "2026-03-31T15:00:00"),
        ("晨间回顾", "个人", "2026-03-31T09:30:00", "2026-03-31T10:00:00"),
        ("范围外事件", "工作", "2026-04-01T09:00:00", "2026-04-01T10:00:00"),
    ):
        db.insert_item(
            {
                "type": "event",
                "owner_id": owner_id,
                "title": title,
                "category": category,
                "start_time": start_time,
                "end_time": end_time,
            }
        )

    data = stats_api.event_stats(
        range_str="2026-03-31..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]
    by_category = {item["category"]: item["count"] for item in data["by_category"]}

    assert sum(item["count"] for item in data["weekly"]) == 2
    assert by_category == {"个人": 1, "工作": 1}
    assert sum(item["count"] for item in data["weekday_slots"]) == 2
    assert {item["slot"] for item in data["time_slots"]} == {"09-12", "14-18"}


def test_timestamp_stats_group_aware_values_in_user_timezone(db: Database) -> None:
    """带偏移时间按用户自然日和小时分桶，不能交给 SQLite 隐式转 UTC。"""

    owner_id = "u-stats-aware-user-zone"
    db.update_user_settings(owner_id, {"timezone": "Asia/Shanghai"})
    for payload in (
        {
            "id": "aware-event",
            "type": "event",
            "title": "上海早会",
            "start_time": "2026-03-30T23:30:00+00:00",
        },
        {
            "id": "aware-task",
            "type": "task",
            "title": "上海当天任务",
            "status": "open",
            "created_at": "2026-03-30T23:30:00+00:00",
        },
        {
            "id": "aware-note",
            "type": "note",
            "title": "上海当天笔记",
            "created_at": "2026-03-30T23:30:00+00:00",
        },
    ):
        db.insert_item({"owner_id": owner_id, **payload})

    event_data = stats_api.event_stats(
        range_str="2026-03-31..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]
    task_data = stats_api.task_stats(
        range_str="2026-03-31..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]
    heatmap = stats_api.activity_heatmap(year=2026, owner_id=owner_id, db=db)["data"]
    march_30 = next(row for row in heatmap["days"] if row["date"] == "2026-03-30")
    march_31 = next(row for row in heatmap["days"] if row["date"] == "2026-03-31")

    assert event_data["time_slots"] == [{"slot": "06-09", "count": 1}]
    assert task_data["totals"]["open"] == 1
    assert march_30["count"] == 0
    assert (march_31["event"], march_31["task"], march_31["note"]) == (1, 1, 1)


def test_event_stats_counts_graph_leaves_and_uses_trimmed_collection_category(
    db: Database,
) -> None:
    """事件图只统计叶子；未分类叶子应回退到清理后的集合分类。"""

    owner_id = "u-event-stats-graph"
    db.create_event_collection(
        {
            "id": "stat-conf",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "统计会议",
            "category": " 学术 ",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-03-06T10:00:00",
        }
    )
    for item_id, title, start_time in (
        ("stat-conf_m01", "摘要截止", "2026-03-05T09:00:00"),
        ("stat-conf_m02", "会议开始", "2026-03-06T10:00:00"),
    ):
        db.insert_item(
            {
                "id": item_id,
                "type": "event",
                "owner_id": owner_id,
                "title": title,
                "category": " 未分类 ",
                "start_time": start_time,
                "event_role": "multi_node_child",
                "event_collection_id": "stat-conf",
                "event_collection_kind": "multi_node",
            }
        )

    data = stats_api.event_stats(
        range_str="2026-03-01..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert sum(item["count"] for item in data["weekly"]) == 2
    assert data["by_category"] == [{"category": "学术", "count": 2}]


def test_task_stats_separates_created_done_and_cancelled_weeks(db: Database) -> None:
    """任务创建、完成和取消时间轴独立统计，详情分布只扫描一次。"""

    owner_id = "u-task-stats"
    for item in (
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "已完成任务",
            "status": "done",
            "category": "工作",
            "priority": 5,
            "created_at": "2026-03-03T09:00:00",
            "completed_at": "2026-03-10T10:00:00",
        },
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "计划任务",
            "status": "open",
            "category": "生活",
            "priority": 3,
            "plan_date": "2026-03-20",
            "created_at": "2026-03-17T09:00:00",
        },
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "仅截止日任务",
            "status": "open",
            "category": " 未分类 ",
            "priority": 1,
            "deadline_at": "2026-03-25T18:00:00",
            "created_at": "2026-03-18T09:00:00",
        },
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "无日期计划任务",
            "status": "open",
            "category": "工作",
            "priority": 2,
            "created_at": "2026-03-19T09:00:00",
        },
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "已取消任务",
            "status": "cancelled",
            "category": "归档",
            "priority": 1,
            "created_at": "2026-02-01T09:00:00",
            "cancelled_at": "2026-03-12T10:00:00",
        },
    ):
        db.insert_item(item)

    data = stats_api.task_stats(
        range_str="2026-03-01..2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]
    weekly = {item["week"]: item for item in data["weekly"]}

    assert weekly["2026-W09"]["created"] == 1
    assert weekly["2026-W10"]["done"] == 1
    assert weekly["2026-W10"]["cancelled"] == 1
    assert weekly["2026-W11"]["created"] == 3
    assert data["totals"] == {"open": 3, "done": 1, "cancelled": 1, "closed": 2}
    assert data["new_this_week"] == 4
    assert data["by_plan"] == [
        {"plan": "2026-03-20", "count": 1},
        {"plan": "2026-03-25", "count": 1},
    ]
    assert data["by_category"] == [
        {"category": "工作", "count": 1},
        {"category": "生活", "count": 1},
    ]
    assert data["by_priority"] == [
        {"priority": 1, "count": 2},
        {"priority": 2, "count": 1},
        {"priority": 3, "count": 1},
        {"priority": 5, "count": 1},
    ]


def test_diary_overview_accepts_explicit_range_and_auto_cadence(db: Database) -> None:
    """日记概览应透传显式范围，并自动选择周粒度。"""

    owner_id = "u-diary-stats"
    for item_id, diary_date, mood, content in (
        ("d1", "2026-01-06", "calm", "第一条记录"),
        ("d2", "2026-01-15", "happy", "第二条记录更长一些"),
        ("d3", "2026-02-03", "happy", "第三条记录"),
    ):
        db.insert_item(
            {
                "id": item_id,
                "type": "diary",
                "owner_id": owner_id,
                "title": item_id,
                "content": content,
                "diary_date": diary_date,
                "mood": mood,
                "created_at": f"{diary_date}T20:00:00",
                "updated_at": f"{diary_date}T20:00:00",
            }
        )

    data = stats_api.diary_overview(
        start_date="2026-01-01",
        end_date="2026-02-15",
        cadence_granularity="auto",
        today="2026-02-15",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert data["summary"]["entry_count"] == 3
    assert data["summary"]["range_start"] == "2026-01-01"
    assert data["summary"]["range_end"] == "2026-02-15"
    assert data["cadence_granularity"] == "week"
    assert sum(item["count"] for item in data["cadence"]) == 3
    assert data["mood_breakdown"][0]["mood"] == "happy"


def test_diary_overview_translates_builder_validation_error(db: Database) -> None:
    """日记分析器的非法反向区间应转换为稳定的 HTTP 400。"""

    error = _assert_http_error(
        400,
        lambda: stats_api.diary_overview(
            start_date="2026-03-31",
            end_date="2026-03-01",
            owner_id="u-diary-invalid",
            db=db,
        ),
    )
    assert error.detail


def test_notes_overview_accepts_explicit_range_and_trims_filters(db: Database) -> None:
    """笔记概览按范围聚合，并清理分类和标签查询参数。"""

    owner_id = "u-note-stats"
    for item_id, title, category, created_at in (
        ("n-in-1", "范围内一", "工作", "2026-03-03T09:00:00"),
        ("n-in-2", "范围内二", "生活", "2026-03-15T09:00:00"),
        ("n-out", "范围外", "归档", "2026-02-20T09:00:00"),
    ):
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": title,
                "content": f"{title} 内容",
                "category": category,
                "tags": ["alpha", "beta"] if item_id != "n-out" else ["legacy"],
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    data = stats_api.notes_overview(
        start_date="2026-03-01",
        end_date="2026-03-31",
        today="2026-03-31",
        category="   ",
        tags="   ",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert data["summary"]["total_count"] == 2
    assert data["cadence_granularity"] == "week"
    assert {item["category"] for item in data["categories"]} == {"工作", "生活"}
    assert {item["tag"] for item in data["hot_tags"]} == {"alpha", "beta"}
    assert sum(item["count"] for item in data["cadence"]) == 2


def test_notes_overview_translates_builder_validation_error(db: Database) -> None:
    """笔记分析器的缺失范围边界应转换为稳定的 HTTP 400。"""

    error = _assert_http_error(
        400,
        lambda: stats_api.notes_overview(
            start_date="2026-03-01",
            owner_id="u-note-invalid",
            db=db,
        ),
    )
    assert "provided together" in error.detail


def test_ledger_stats_explicit_bounds_override_range_preset(db: Database) -> None:
    """成对显式日期应优先于 range 预设。"""

    owner_id = "u-ledger-bounds"
    for title, amount, ledger_date in (
        ("一月支出", 88, "2026-01-05"),
        ("三月支出", 66, "2026-03-18"),
    ):
        db.insert_item(
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": title,
                "amount": amount,
                "transaction_type": "expense",
                "ledger_category": "测试",
                "ledger_date": ledger_date,
            }
        )

    data = stats_api.ledger_stats(
        range_str="all",
        start_date="2026-03-01",
        end_date="2026-03-31",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert data["expense_by_category"] == [{"category": "测试", "total": 66}]
    assert data["daily"] == [{"date": "2026-03-18", "income": 0, "expense": 66}]


def test_notes_overview_uses_yearly_cadence_for_cross_year_ranges(db: Database) -> None:
    """跨年范围应自动切换为年度节奏。"""

    owner_id = "u-note-stats-year"
    for item_id, created_at in (
        ("n-2024", "2024-03-03T09:00:00"),
        ("n-2025", "2025-06-18T09:00:00"),
        ("n-2026", "2026-02-01T09:00:00"),
    ):
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": item_id,
                "content": f"{item_id} 内容",
                "category": "工作",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    data = stats_api.notes_overview(
        start_date="2024-01-01",
        end_date="2026-12-31",
        today="2026-12-31",
        owner_id=owner_id,
        db=db,
    )["data"]

    assert data["cadence_granularity"] == "year"
    assert [item["label"] for item in data["cadence"]] == ["2024", "2025", "2026"]
    assert [item["count"] for item in data["cadence"]] == [1, 1, 1]


@pytest.mark.parametrize(
    ("range_value", "expected"),
    (
        (None, ("2026-05-01", "2026-05-20")),
        ("   ", ("2026-05-01", "2026-05-20")),
        ("week", ("2026-05-18", "2026-05-20")),
        ("quarter", ("2026-04-01", "2026-05-20")),
        ("year", ("2026-01-01", "2026-05-20")),
        ("last_year", ("2025-01-01", "2025-12-31")),
        ("all", ("1970-01-01", "2026-05-20")),
        ("2024-02", ("2024-02-01", "2024-02-29")),
        ("2026-03-01..2026-03-31", ("2026-03-01", "2026-03-31")),
    ),
)
def test_parse_range_supports_presets_months_and_explicit_bounds(
    range_value: str | None,
    expected: tuple[str, str],
) -> None:
    """所有公开范围语法都应解析为真实、规范化的闭区间。"""

    assert stats_api._parse_range(range_value, today=date(2026, 5, 20)) == expected


@pytest.mark.parametrize(
    "range_value",
    (
        "unsupported",
        "2026-13",
        "2026-03-31..2026-03-01",
        "bad..2026-03-01",
        "2026-01-01..2026-02-01..2026-03-01",
    ),
)
def test_parse_range_rejects_invalid_or_reversed_values(range_value: str) -> None:
    """非法月份、日期、分隔符和反向区间必须失败关闭。"""

    with pytest.raises(ValueError):
        stats_api._parse_range(range_value, today=date(2026, 5, 20))


def test_stats_range_requires_paired_explicit_bounds() -> None:
    """显式开始和结束日期不能只给一端。"""

    error = _assert_http_error(
        422,
        lambda: stats_api._resolve_stats_range("all", start_date="2026-01-01"),
    )
    assert "同时提供" in error.detail


def test_amount_histogram_uses_half_open_boundaries() -> None:
    """相邻金额桶边界不应重复计数。"""

    histogram = stats_api._build_amount_histogram([0, 19.99, 20, 49.99, 50, 1000])
    assert [item["count"] for item in histogram] == [2, 2, 1, 0, 0, 1]


def test_ledger_comparison_fills_gaps_and_keeps_mom_and_yoy_baselines(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """月度比较填充空月，并从同一查询提供环比和同比基线。"""

    owner_id = "u-ledger-compare"
    monkeypatch.setattr(stats_api, "_today", lambda *_args: date(2026, 3, 29))
    for title, amount, transaction_type, ledger_date in (
        ("同比十月支出", 40, "expense", "2024-10-12"),
        ("同比十月收入", 100, "income", "2024-10-15"),
        ("九月支出", 90, "expense", "2025-09-20"),
        ("十月支出", 120, "expense", "2025-10-12"),
        ("十月收入", 500, "income", "2025-10-25"),
        ("十二月支出", 360, "expense", "2025-12-08"),
        ("二月支出", 240, "expense", "2026-02-18"),
        ("三月支出", 180, "expense", "2026-03-05"),
        ("三月转账", 10, "transfer", "2026-03-06"),
        ("未来支出", 999, "expense", "2026-04-05"),
    ):
        db.insert_item(
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": title,
                "amount": amount,
                "transaction_type": transaction_type,
                "ledger_category": "餐饮",
                "account_name": "现金",
                "counter_account_name": "银行卡" if transaction_type == "transfer" else None,
                "ledger_date": ledger_date,
            }
        )

    months = stats_api.ledger_comparison(months=6, owner_id=owner_id, db=db)["data"]["months"]

    assert [item["month"] for item in months] == [
        "2025-10",
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
        "2026-03",
    ]
    assert months[0]["expense"] == 120
    assert months[0]["income"] == 500
    assert months[0]["prev_expense"] == 90
    assert months[0]["yoy_expense"] == 40
    assert months[0]["yoy_income"] == 100
    assert months[1]["expense"] == 0
    assert months[3]["expense"] == 0
    assert months[5]["prev_expense"] == 240


def test_ledger_comparison_rejects_direct_out_of_range_months(db: Database) -> None:
    """内部直接调用也必须遵守 3 到 12 个月的公开边界。"""

    _assert_http_error(
        422,
        lambda: stats_api.ledger_comparison(months=2, owner_id="u", db=db),
    )


def test_activity_heatmap_counts_all_item_types_and_leap_day(db: Database) -> None:
    """全年热力图一次查询聚合五类条目，并保留闰日空位。"""

    owner_id = "u-heatmap"
    for item in (
        {
            "type": "ledger",
            "owner_id": owner_id,
            "title": "支出",
            "amount": 1,
            "transaction_type": "expense",
            "ledger_date": "2024-02-29",
        },
        {
            "type": "task",
            "owner_id": owner_id,
            "title": "任务",
            "created_at": "2024-02-29T08:00:00",
        },
        {
            "type": "event",
            "owner_id": owner_id,
            "title": "日程",
            "start_time": "2024-02-29T09:00:00",
        },
        {
            "type": "note",
            "owner_id": owner_id,
            "title": "笔记",
            "created_at": "2024-02-29T10:00:00",
        },
        {
            "type": "diary",
            "owner_id": owner_id,
            "title": "日记",
            "diary_date": "2024-02-29",
        },
        {
            "type": "task",
            "owner_id": "other-owner",
            "title": "他人任务",
            "created_at": "2024-02-29T08:00:00",
        },
    ):
        db.insert_item(item)

    data = stats_api.activity_heatmap(year=2024, owner_id=owner_id, db=db)["data"]
    leap_day = next(item for item in data["days"] if item["date"] == "2024-02-29")

    assert len(data["days"]) == 366
    assert leap_day == {
        "date": "2024-02-29",
        "count": 5,
        "ledger": 1,
        "task": 1,
        "event": 1,
        "note": 1,
        "diary": 1,
    }


def test_activity_heatmap_rejects_invalid_direct_year(db: Database) -> None:
    """HTTP 之外的调用也不能构造 SQLite 无意义年份。"""

    _assert_http_error(
        422,
        lambda: stats_api.activity_heatmap(year=1969, owner_id="u", db=db),
    )
