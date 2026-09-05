"""Pendo Web 全文搜索的筛选、分页、集合补全与数据库回归。"""

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.pendo.services.db import Database
from plugins.pendo.web.api import search as search_api
from tests.helpers.assertions import assert_http_error as _assert_http_error


def test_search_router_registers_only_the_search_endpoint() -> None:
    """搜索模块只应注册一个 GET 入口。"""

    registered = {
        (route.path, frozenset(getattr(route, "methods", set())))
        for route in search_api.router.routes
    }
    assert registered == {("/search", frozenset({"GET"}))}


def test_search_http_endpoint_preserves_type_alias_and_query_bounds(db: Database) -> None:
    """HTTP 层继续接收 ``type`` 参数，并让声明式分页约束在路由前生效。"""

    owner_id = "owner-search-http"
    db.insert_item(
        {
            "id": "search-http-task",
            "owner_id": owner_id,
            "type": "task",
            "title": "HTTP 搜索待办",
            "plan_date": "2030-01-01",
        }
    )
    app = FastAPI()
    app.include_router(search_api.router)
    app.dependency_overrides[search_api.get_current_user] = lambda: owner_id
    app.dependency_overrides[search_api.get_db]           = lambda: db

    with TestClient(app) as client:
        response = client.get(
            "/search",
            params={"q": "HTTP 搜索", "type": "task", "page_size": 1},
        )
        invalid_page = client.get("/search", params={"q": "HTTP", "page": 0})

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["id"] == "search-http-task"
    assert invalid_page.status_code == 422


def test_search_filter_builder_trims_values_and_infers_specialized_types() -> None:
    """类型专属筛选应推断目标类型，分类别名和空白值保持一致语义。"""

    assert search_api._build_search_filters(
        " ledger ",
        " 餐饮 ",
        None,
        None,
        " EXPENSE ",
        " 现金 ",
        " 小店 ",
    ) == {
        "type": "ledger",
        "ledger_category": "餐饮",
        "transaction_type": "expense",
        "account_name": "现金",
        "merchant": "小店",
    }
    assert search_api._build_search_filters(None, None, "餐饮", None, None, None, None) == {
        "type": "ledger",
        "ledger_category": "餐饮",
    }
    assert search_api._build_search_filters(None, None, None, " OPEN ", None, None, None) == {
        "type": "task",
        "status": "open",
    }
    assert search_api._build_search_filters(None, " ", None, " ", None, "", " ") == {}


@pytest.mark.parametrize(
    "kwargs",
    (
        {"item_type": "unknown"},
        {"status": "unknown"},
        {"transaction_type": "refund"},
        {"category": "普通", "ledger_category": "账目"},
        {"item_type": "note", "ledger_category": "账目"},
        {"item_type": "event", "status": "open"},
        {"status": "open", "account_name": "现金"},
    ),
)
def test_invalid_search_filter_combinations_return_422(
    db: Database,
    kwargs: dict[str, str],
) -> None:
    """未知枚举和跨类型组合不能静默返回空搜索结果。"""

    _assert_http_error(
        422,
        lambda: search_api.search_items(q="关键词", owner_id="owner-search-error", db=db, **kwargs),
    )


@pytest.mark.parametrize(
    "kwargs",
    (
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
    ),
)
def test_invalid_direct_search_pagination_returns_422(
    db: Database,
    kwargs: dict[str, int],
) -> None:
    """直接调用也必须执行与 FastAPI 声明相同的分页边界。"""

    _assert_http_error(
        422,
        lambda: search_api.search_items(q="关键词", owner_id="owner-page-error", db=db, **kwargs),
    )


def test_empty_search_query_returns_422_before_database_access(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯空白关键词应在数据库调用前失败。"""

    called = False

    def unexpected_search(*_args: object, **_kwargs: object) -> tuple[list[object], int]:
        nonlocal called
        called = True
        return [], 0

    monkeypatch.setattr(db, "search_items_page", unexpected_search)

    error = _assert_http_error(
        422,
        lambda: search_api.search_items(q="   ", owner_id="owner-empty-query", db=db),
    )
    assert error.detail == "Search query cannot be empty"
    assert called is False


def test_search_route_pages_results_and_preserves_total(db: Database) -> None:
    """当前页和完整总数应分离。"""

    owner_id = "owner-search-pages"
    for index in range(3):
        db.insert_item(
            {
                "id": f"search-page-{index}",
                "owner_id": owner_id,
                "type": "note",
                "title": f"共同关键词 {index}",
                "content": "分页验证",
            }
        )

    response = search_api.search_items(
        q         = "共同关键词",
        page      = 2,
        page_size = 1,
        owner_id  = owner_id,
        db        = db,
    )
    data = cast(dict[str, Any], response["data"])

    assert data["total"] == 3
    assert data["page"] == 2
    assert data["page_size"] == 1
    assert len(cast(list[dict[str, Any]], data["items"])) == 1


def test_search_route_batches_event_collection_payloads(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同页多个日程集合摘要应通过一次所有者范围批量读取补齐。"""

    owner_id = "owner-search-batch-collections"
    db.create_event_collection(
        {
            "id": "search-collection",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "批量会议",
        }
    )
    for index in range(2):
        db.insert_item(
            {
                "id": f"search-event-{index}",
                "owner_id": owner_id,
                "type": "event",
                "title": f"共同议程 {index}",
                "start_time": f"2030-09-0{index + 1}T09:00:00",
                "event_collection_id": "search-collection",
                "event_collection_kind": "multi_node",
                "event_role": "multi_node_child",
            }
        )

    original_batch                     = db.get_event_collections_by_ids
    calls: list[tuple[str, list[str]]] = []

    def capture_batch(request_owner: str, collection_ids: list[str]) -> dict[str, dict[str, Any]]:
        calls.append((request_owner, list(collection_ids)))
        return original_batch(request_owner, collection_ids)

    monkeypatch.setattr(db, "get_event_collections_by_ids", capture_batch)

    response = search_api.search_items(q="共同议程", owner_id=owner_id, db=db)
    rows = cast(list[dict[str, Any]], cast(dict[str, Any], response["data"])["items"])

    assert calls == [(owner_id, ["search-collection", "search-collection"])]
    assert len(rows) == 2
    assert {cast(dict[str, Any], row["collection"])["title"] for row in rows} == {"批量会议"}


def test_event_collection_category_search_trims_legacy_values_and_keeps_total_aligned(
    db: Database,
) -> None:
    """旧集合分类的首尾空格不能造成搜索总数有值但当前页为空。"""

    owner_id      = "owner-search-legacy-category"
    collection_id = db.create_event_collection(
        {
            "id": "legacy-category-collection",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "旧分类会议",
            "category": "学术",
        }
    )
    db.insert_item(
        {
            "id": "legacy-category-event",
            "owner_id": owner_id,
            "type": "event",
            "title": "子节点标题",
            "category": "其他",
            "start_time": "2030-10-01T09:00:00",
            "event_collection_id": collection_id,
            "event_collection_kind": "multi_node",
            "event_role": "multi_node_child",
        }
    )
    with db.get_connection():
        db.get_connection().execute(
            "UPDATE event_collections SET category = ' 学术 ' WHERE id = ?",
            (collection_id,),
        )

    response = search_api.search_items(
        q         = "旧分类会议",
        item_type = "event",
        category  = " 学术 ",
        owner_id  = owner_id,
        db        = db,
    )
    data = cast(dict[str, Any], response["data"])
    rows = cast(list[dict[str, Any]], data["items"])

    assert data["total"] == 1
    assert [row["id"] for row in rows] == ["legacy-category-event"]


def test_database_search_items_matches_additional_text_fields(db: Database) -> None:
    """LIKE 兜底搜索应覆盖日程地点和日记天气等扩展文本列。"""

    owner_id = "u-search"
    db.insert_item(
        {
            "id": "ev1",
            "owner_id": owner_id,
            "type": "event",
            "title": "组会",
            "content": "",
            "location": "图书馆 402",
            "start_time": "2026-03-28T09:00:00",
            "end_time": "2026-03-28T10:00:00",
        }
    )
    db.insert_item(
        {
            "id": "dy1",
            "owner_id": owner_id,
            "type": "diary",
            "title": "雨夜",
            "content": "今天走得很慢。",
            "weather": "🌧️ 雨",
            "location": "窗边的风声",
            "diary_date": "2026-03-28",
        }
    )

    by_location, location_total = db.search_items_page(owner_id, "图书馆", limit=10)
    by_weather, weather_total = db.search_items_page(
        owner_id,
        "风声",
        filters = {"type": "diary"},
        limit   = 10,
    )

    assert location_total == 1
    assert weather_total == 1
    assert [item.id for item in by_location] == ["ev1"]
    assert [item.id for item in by_weather] == ["dy1"]


def test_database_search_preserves_fts_rank_before_newer_like_only_rows(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FTS 相关性结果应排在 LIKE 补充结果前，并保持去重。"""
    owner_id = "u-search-order"
    db.insert_item(
        {
            "id": "fts-first",
            "owner_id": owner_id,
            "type": "note",
            "title": "alpha 相关结果",
            "created_at": "2030-01-01T08:00:00",
            "updated_at": "2030-01-01T08:00:00",
        }
    )
    db.insert_item(
        {
            "id": "like-newer",
            "owner_id": owner_id,
            "type": "note",
            "title": "alpha 新结果",
            "created_at": "2040-01-01T08:00:00",
            "updated_at": "2040-01-01T08:00:00",
        }
    )
    monkeypatch.setattr(db, "_search_fts_ids", lambda *_args, **_kwargs: ["fts-first"])

    results, total = db.search_items_page(
        owner_id,
        "alpha",
        filters = {"type": "note"},
        limit   = 10,
    )

    assert total == 2
    assert [item.id for item in results] == ["fts-first", "like-newer"]


def test_database_search_items_supports_ledger_category_filter(db: Database) -> None:
    """账目搜索应支持专属分类，并把转账两侧账户都纳入账户筛选。"""

    owner_id = "u-search-ledger"
    db.insert_item(
        {
            "id": "ld1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "餐饮消费",
            "amount": 20,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-28",
        }
    )
    db.insert_item(
        {
            "id": "ld2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "交通消费",
            "amount": 8,
            "transaction_type": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-28",
        }
    )
    db.insert_item(
        {
            "id": "ld3",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "信用卡还款",
            "amount_cents": 100000,
            "transaction_type": "transfer",
            "ledger_category": "转账",
            "ledger_date": "2026-03-28",
            "account_name": "微信",
            "counter_account_name": "招行信用卡",
        }
    )

    results, result_total = db.search_items_page(
        owner_id,
        "消费",
        filters = {"type": "ledger", "ledger_category": "餐饮"},
        limit   = 10,
    )
    transfer_results, transfer_total = db.search_items_page(
        owner_id,
        "还款",
        filters = {"type": "ledger", "account_name": "招行信用卡"},
        limit   = 10,
    )
    route_response = search_api.search_items(
        q               = "消费",
        item_type       = "ledger",
        ledger_category = " 餐饮 ",
        owner_id        = owner_id,
        db              = db,
    )
    route_data = cast(dict[str, Any], route_response["data"])

    assert result_total == 1
    assert transfer_total == 1
    assert [item.id for item in results] == ["ld1"]
    assert [item.id for item in transfer_results] == ["ld3"]
    assert [item["id"] for item in cast(list[dict[str, Any]], route_data["items"])] == ["ld1"]


def test_database_search_items_matches_event_collection_text(db: Database) -> None:
    """集合标题命中时应返回其有效日程叶子。"""

    owner_id = "u-search-event-collection"
    db.create_event_collection(
        {
            "id": "col-frb",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "FRB2026会议",
            "category": "学术",
            "notes": "整体会议信息",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-04-01T10:00:00",
        }
    )
    db.insert_item(
        {
            "id": "col-frb_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "摘要截止",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "col-frb",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        }
    )

    results, total = db.search_items_page(
        owner_id,
        "FRB2026",
        filters = {"type": "event"},
        limit   = 10,
    )

    assert total == 1
    assert [item.id for item in results] == ["col-frb_m01"]


def test_search_route_adds_event_collection_payload(db: Database) -> None:
    """路由结果应附带经过公开字段白名单裁剪的集合摘要。"""

    owner_id = "u-search-route-collection"
    db.create_event_collection(
        {
            "id": "col-import",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "导入会议",
            "category": "学术",
            "notes": "整体备注",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-04-01T10:00:00",
        }
    )
    db.insert_item(
        {
            "id": "col-import_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "摘要截止",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "col-import",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        }
    )

    result = search_api.search_items(
        q         = "导入会议",
        item_type = "event",
        owner_id  = owner_id,
        db        = db,
    )
    data = cast(dict[str, Any], result["data"])
    item = cast(list[dict[str, Any]], data["items"])[0]

    assert item["id"] == "col-import_m01"
    assert cast(dict[str, Any], item["collection"])["title"] == "导入会议"
