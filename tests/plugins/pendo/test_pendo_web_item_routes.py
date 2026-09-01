"""Pendo 通用条目路由的过滤、规范化、并发和日程图回归。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from plugins.pendo.models.item import get_item_type_value
from plugins.pendo.services.db import Database
from plugins.pendo.utils.identifiers import is_canonical_internal_id
from plugins.pendo.utils.validators import normalize_item_fields
from plugins.pendo.web.api import items as items_api
from tests.helpers.assertions import assert_http_error as _assert_http_error


def _normalized_item(owner_id: str, item_type: str, **overrides: Any) -> dict[str, Any]:
    """构造经过生产规范化器校验的最小条目。"""

    payload: dict[str, Any] = {
        "owner_id": owner_id,
        "type": item_type,
        "title": f"{item_type} 条目",
        "created_at": "2030-01-01T08:00:00",
        "updated_at": "2030-01-01T08:00:00",
    }
    payload.update(
        {
            "event": {"start_time": "2030-01-01T09:00:00"},
            "task": {"plan_date": "2030-01-01"},
            "note": {},
            "diary": {"content": "日记正文", "diary_date": "2030-01-01"},
            "ledger": {"amount": 1, "ledger_date": "2030-01-01"},
        }[item_type]
    )
    payload.update(overrides)
    return normalize_item_fields(payload, partial=False)


def _insert_item(
    db: Database,
    owner_id: str,
    item_id: str,
    item_type: str,
    **overrides: Any,
) -> str:
    """按固定 ID 写入一个有效条目。"""

    return db.insert_item(
        _normalized_item(owner_id, item_type, **overrides),
        custom_id=item_id,
    )


def test_item_router_registers_only_the_expected_paths() -> None:
    """通用条目模块应只暴露职责明确的八个端点。"""

    registered = {
        (route.path, frozenset(getattr(route, "methods", set())))
        for route in items_api.router.routes
    }

    assert registered == {
        ("/items/aggregate", frozenset({"GET"})),
        ("/items/categories", frozenset({"GET"})),
        ("/items/ledger/accounts", frozenset({"GET"})),
        ("/items", frozenset({"GET"})),
        ("/items/{item_id}", frozenset({"GET"})),
        ("/items", frozenset({"POST"})),
        ("/items/{item_id}", frozenset({"PUT"})),
        ("/items/{item_id}", frozenset({"DELETE"})),
    }


def test_create_models_do_not_share_mutable_tag_defaults() -> None:
    """不同创建请求不得共享默认标签列表。"""

    first = items_api.ItemCreate(type="note", title="第一条")
    second = items_api.ItemCreate(type="note", title="第二条")

    first.tags.append("只属于第一条")

    assert second.tags == []


def test_small_item_helpers_cover_empty_and_legacy_inputs(db: Database) -> None:
    """纯辅助函数应稳定处理空日期、非法引用形状和空引用集合。"""

    assert items_api._entry_time_for_diary_date("2030-01-01T08:09:10", None) == (
        "2030-01-01T08:09:10"
    )
    assert items_api._entry_time_for_diary_date("short", "2030-01-02") == ("2030-01-02T00:00:00")
    assert items_api._collect_note_reference_ids(
        {
            "references": [None, {}, {"id": "same"}, {"id": "same"}],
            "related_items": ["", "same", "second", "second"],
        }
    ) == ["same", "second"]
    assert items_api._resolve_note_reference_payload(
        db,
        "owner-empty-reference",
        {"references": [], "related_items": []},
    ) == {"references": [], "related_items": []}


def test_event_shift_helper_ignores_invalid_or_reverse_legacy_times() -> None:
    """损坏的旧日程时间不能让无关更新崩溃，也不能产生负持续时长。"""

    invalid_updates = {"start_time": "2030-01-02T09:00:00"}
    items_api._shift_event_end_time_if_start_moved(
        {"start_time": "bad", "end_time": "2030-01-01T10:00:00"},
        invalid_updates,
    )
    assert "end_time" not in invalid_updates

    reverse_updates = {"start_time": "2030-01-02T09:00:00"}
    items_api._shift_event_end_time_if_start_moved(
        {"start_time": "2030-01-01T10:00:00", "end_time": "2030-01-01T09:00:00"},
        reverse_updates,
    )
    assert "end_time" not in reverse_updates

    no_rule_updates = {"start_time": "2030-01-02T09:00:00"}
    items_api._prepare_event_update({"start_time": None, "remind_times": []}, no_rule_updates)
    assert "reminder_rules" not in no_rule_updates


def test_categories_and_accounts_trim_and_deduplicate_legacy_values(db: Database) -> None:
    """旧数据中的首尾空格不能制造重复分类或账户。"""

    owner_id = "owner-trimmed-facets"
    first_note = _insert_item(db, owner_id, "note-a", "note", category="工作")
    second_note = _insert_item(db, owner_id, "note-b", "note", category="生活")
    first_ledger = _insert_item(
        db,
        owner_id,
        "ledger-a",
        "ledger",
        account_name="现金",
        counter_account_name="银行卡",
    )
    second_ledger = _insert_item(
        db,
        owner_id,
        "ledger-b",
        "ledger",
        account_name="银行卡",
        counter_account_name="现金",
    )
    with db.get_connection():
        db.get_connection().execute(
            "UPDATE items SET category = ' 工作 ' WHERE id = ?", (first_note,)
        )
        db.get_connection().execute(
            "UPDATE items SET category = '工作' WHERE id = ?", (second_note,)
        )
        db.get_connection().execute(
            "UPDATE items SET account_name = ' 现金 ', counter_account_name = ' 银行卡 ' WHERE id = ?",
            (first_ledger,),
        )
        db.get_connection().execute(
            "UPDATE items SET account_name = '银行卡', counter_account_name = '现金' WHERE id = ?",
            (second_ledger,),
        )

    categories = items_api.list_categories(type="note", owner_id=owner_id, db=db)
    all_categories = items_api.list_categories(owner_id=owner_id, db=db)
    accounts = items_api.list_ledger_accounts(owner_id=owner_id, db=db)
    filtered_items = items_api.list_items(
        type="note",
        category=" 工作 ",
        owner_id=owner_id,
        db=db,
    )

    assert categories == {"ok": True, "data": {"categories": ["工作"]}, "message": ""}
    assert set(cast(dict[str, list[str]], all_categories["data"])["categories"]) == {
        "工作",
        "未分类",
    }
    assert cast(dict[str, Any], filtered_items["data"])["total"] == 2
    assert accounts["ok"] is True and accounts["message"] == ""
    account_values = cast(dict[str, list[str]], accounts["data"])["accounts"]
    assert len(account_values) == 2
    assert set(account_values) == {"银行卡", "现金"}


def test_empty_account_list_uses_cash_default_and_invalid_category_type_is_422(
    db: Database,
) -> None:
    """空账户列表保留前端默认项，非法类型不能进入动态 SQL。"""

    assert items_api.list_ledger_accounts(owner_id="owner-empty", db=db)["data"] == {
        "accounts": ["现金"]
    }
    error = _assert_http_error(
        422,
        lambda: items_api.list_categories(type="not-a-type", owner_id="owner-empty", db=db),
    )
    assert error.detail == "Invalid type: not-a-type"


@pytest.mark.parametrize(
    ("keyword", "expected_id"),
    (("%", "percent"), ("_", "underscore"), ("\\", "backslash")),
)
def test_literal_keyword_filter_and_total_share_exact_semantics(
    db: Database,
    keyword: str,
    expected_id: str,
) -> None:
    """LIKE 特殊字符按字面量搜索，分页数据与总数必须一致。"""

    owner_id = f"owner-keyword-{expected_id}"
    _insert_item(db, owner_id, "percent", "note", title="包含百分号 %")
    _insert_item(db, owner_id, "underscore", "note", title="包含下划线 _")
    _insert_item(db, owner_id, "backslash", "note", title="包含反斜杠 \\")
    _insert_item(db, owner_id, "plain", "note", title="普通文本")

    response = items_api.list_items(
        type="note",
        keyword=keyword,
        page=1,
        page_size=1,
        owner_id=owner_id,
        db=db,
    )
    data = cast(dict[str, Any], response["data"])
    rows = cast(list[dict[str, Any]], data["items"])

    assert data["total"] == 1
    assert [row["id"] for row in rows] == [expected_id]


def test_tag_filter_escapes_wildcards_in_rows_and_total(db: Database) -> None:
    """标签筛选也必须对 JSON 元素中的通配符做字面量匹配。"""

    owner_id = "owner-tag-wildcard"
    literal_id = _insert_item(db, owner_id, "literal-tag", "note", tags=["literal"])
    _insert_item(db, owner_id, "similar-tag", "note", tags=["axb"])
    with db.get_connection():
        db.get_connection().execute(
            "UPDATE items SET tags = ? WHERE id = ?",
            ('["a%b"]', literal_id),
        )

    response = items_api.list_items(
        type="note",
        tags="a%b",
        page_size=1,
        owner_id=owner_id,
        db=db,
    )
    data = cast(dict[str, Any], response["data"])
    rows = cast(list[dict[str, Any]], data["items"])

    assert data["total"] == 1
    assert [row["id"] for row in rows] == ["literal-tag"]


def test_amount_filters_use_integer_cents_for_list_total_and_aggregate(db: Database) -> None:
    """半分边界统一为整数分，列表、总数和汇总不得漂移。"""

    owner_id = "owner-cent-filters"
    _insert_item(
        db,
        owner_id,
        "one-cent",
        "ledger",
        amount=0.01,
        transaction_type="expense",
    )
    _insert_item(
        db,
        owner_id,
        "one-twenty-three",
        "ledger",
        amount=1.23,
        transaction_type="income",
    )
    _insert_item(
        db,
        owner_id,
        "two-fifty",
        "ledger",
        amount=2.5,
        transaction_type="expense",
    )

    listed = items_api.list_items(
        amount_min=0.005,
        amount_max=1.234,
        page_size=1,
        owner_id=owner_id,
        db=db,
    )
    listed_data = cast(dict[str, Any], listed["data"])
    summary = items_api.aggregate_items(
        amount_min=0.005,
        amount_max=1.234,
        owner_id=owner_id,
        db=db,
    )

    assert listed_data["total"] == 2
    assert cast(dict[str, Any], summary["data"]) == {
        "income": 1.23,
        "expense": 0.01,
        "transfer": 0.0,
        "balance": 1.22,
        "count": 2,
    }


def test_database_amount_filter_uses_decimal_half_up_rounding(db: Database) -> None:
    """数据层直接调用也必须把半分上限按唯一的十进制规则舍入为一分。"""

    owner_id = "owner-direct-cent-filter"
    _insert_item(db, owner_id, "one-cent-direct", "ledger", amount=0.01)

    items = db.get_items(
        owner_id,
        filters={"type": "ledger", "amount_max": 0.005},
        use_cache=False,
    )

    assert [item.id for item in items] == ["one-cent-direct"]


def test_legacy_ledger_amount_fallback_covers_filters_and_aggregates(db: Database) -> None:
    """旧账目缺少 amount_cents 时，统计和金额筛选仍使用 amount。"""

    owner_id = "owner-legacy-amount"
    legacy_id = _insert_item(
        db,
        owner_id,
        "legacy-ledger",
        "ledger",
        amount=12.34,
        transaction_type="expense",
        ledger_date="2030-01-02",
    )
    with db.get_connection():
        db.get_connection().execute(
            "UPDATE items SET amount_cents = NULL WHERE id = ?",
            (legacy_id,),
        )

    items = db.get_items(
        owner_id,
        filters={"type": "ledger", "amount_min": 12.34, "amount_max": 12.34},
        use_cache=False,
    )
    assert [item.id for item in items] == [legacy_id]
    assert db.aggregate_item_amounts(owner_id, {"type": "ledger"}) == {
        "expense": (1234, 1),
    }
    assert db.aggregate_ledger_amounts_by_day(owner_id, {"type": "ledger"}) == {
        "2030-01-02": (1234, 0),
    }


def test_aggregate_applies_category_and_counts_unknown_legacy_kinds(db: Database) -> None:
    """分类条件应参与共享汇总；旧交易类型只计数，不能污染收支金额。"""

    owner_id = "owner-aggregate-legacy"
    valid_id = _insert_item(
        db,
        owner_id,
        "food-expense",
        "ledger",
        amount=10,
        ledger_category="餐饮",
        transaction_type="expense",
    )
    _insert_item(
        db,
        owner_id,
        "other-expense",
        "ledger",
        amount=30,
        ledger_category="其他",
        transaction_type="expense",
    )

    category_summary = items_api.aggregate_items(
        category=" 餐饮 ",
        owner_id=owner_id,
        db=db,
    )
    assert cast(dict[str, Any], category_summary["data"])["expense"] == 10.0

    with db.get_connection():
        db.get_connection().execute(
            "UPDATE items SET transaction_type = 'legacy' WHERE id = ?",
            (valid_id,),
        )
    legacy_summary = items_api.aggregate_items(
        category="餐饮",
        owner_id=owner_id,
        db=db,
    )
    assert cast(dict[str, Any], legacy_summary["data"]) == {
        "income": 0.0,
        "expense": 0.0,
        "transfer": 0.0,
        "balance": 0.0,
        "count": 1,
    }


@pytest.mark.parametrize(
    "kwargs",
    (
        {"amount_min": float("nan")},
        {"amount_max": float("inf")},
        {"amount_min": 2, "amount_max": 1},
    ),
)
def test_invalid_amount_ranges_return_422(db: Database, kwargs: dict[str, float]) -> None:
    """非有限数值和反向区间不能下沉到 SQLite。"""

    _assert_http_error(
        422,
        lambda: items_api.list_items(owner_id="owner-invalid-amount", db=db, **kwargs),
    )


def test_aggregate_is_ledger_only_and_validates_transaction_type(db: Database) -> None:
    """金额汇总拒绝非账目类型和未知交易类型。"""

    _assert_http_error(
        422,
        lambda: items_api.aggregate_items(type="note", owner_id="owner-aggregate", db=db),
    )
    _assert_http_error(
        422,
        lambda: items_api.aggregate_items(
            transaction_type="refund",
            owner_id="owner-aggregate",
            db=db,
        ),
    )


def test_date_only_event_range_expands_to_the_whole_day(db: Database) -> None:
    """日期形式的 datetime 边界应覆盖当天末秒，日期列本身保持原值。"""

    owner_id = "owner-date-boundary"
    _insert_item(
        db,
        owner_id,
        "last-second",
        "event",
        start_time="2030-05-01T23:59:59",
    )
    _insert_item(
        db,
        owner_id,
        "next-day",
        "event",
        start_time="2030-05-02T00:00:00",
    )

    response = items_api.list_items(
        type="event",
        date_range="2030-05-01..2030-05-01",
        owner_id=owner_id,
        db=db,
    )
    data = cast(dict[str, Any], response["data"])
    rows = cast(list[dict[str, Any]], data["items"])

    assert data["total"] == 1
    assert [row["id"] for row in rows] == ["last-second"]
    assert items_api._resolve_date_filters("diary", None, "2030-05-01", "2030-05-01") == {
        "date_field": "diary_date",
        "start_date": "2030-05-01",
        "end_date": "2030-05-01",
    }
    assert items_api._resolve_date_filters(
        "event",
        None,
        "2030-05-01T08:00:00",
        "2030-05-01T09:00:00",
    ) == {
        "date_field": "start_time",
        "start_date": "2030-05-01T08:00:00",
        "end_date": "2030-05-01T09:00:00",
    }


@pytest.mark.parametrize(
    "kwargs",
    (
        {"date_range": "2030-01-01"},
        {
            "date_range": "2030-01-01..2030-01-02",
            "start_date": "2030-01-01",
            "end_date": "2030-01-02",
        },
        {"start_date": "2030-01-01"},
        {"start_date": "2030-01-02", "end_date": "2030-01-01"},
        {"start_date": "not-a-date", "end_date": "2030-01-01"},
        {"start_date": "2030-01-01", "end_date": "2030-01-02T00:00:00+00:00"},
        {"type": "note", "date_field": "start_time"},
    ),
)
def test_invalid_date_filter_shapes_return_422(db: Database, kwargs: dict[str, str]) -> None:
    """冲突、残缺、反向或跨类型日期参数应明确报错。"""

    _assert_http_error(
        422,
        lambda: items_api.list_items(owner_id="owner-invalid-date", db=db, **kwargs),
    )


def test_item_timestamp_filter_and_sort_use_user_timezone_and_real_instants(
    db: Database,
) -> None:
    owner_id = "owner-mixed-timestamp-list"
    db.update_user_settings(owner_id, {"timezone": "Asia/Shanghai"})
    _insert_item(
        db,
        owner_id,
        "later-instant",
        "event",
        start_time="2030-05-01T23:00:00+00:00",
    )
    _insert_item(
        db,
        owner_id,
        "earlier-instant",
        "event",
        start_time="2030-05-02T01:00:00+08:00",
    )

    response = items_api.list_items(
        type="event",
        date_range="2030-05-02..2030-05-02",
        sort="start_time",
        order="asc",
        owner_id=owner_id,
        db=db,
    )
    rows = cast(list[dict[str, Any]], cast(dict[str, Any], response["data"])["items"])

    assert [row["id"] for row in rows] == ["earlier-instant", "later-instant"]


@pytest.mark.parametrize(
    "kwargs",
    (
        {"type": "unknown"},
        {"status": "unknown"},
        {"transaction_type": "refund"},
        {"priority": 0},
        {"priority": 6},
        {"status": "open", "amount_min": 1},
        {"type": "event", "status": "open"},
        {"type": "note", "amount_min": 1},
        {"page": 0},
        {"page_size": 101},
        {"sort": "id"},
        {"order": "sideways"},
    ),
)
def test_invalid_list_filter_combinations_return_422(
    db: Database,
    kwargs: dict[str, Any],
) -> None:
    """未知枚举、越界分页和跨类型组合不能静默返回空列表。"""

    _assert_http_error(
        422,
        lambda: items_api.list_items(owner_id="owner-invalid-filter", db=db, **kwargs),
    )


@pytest.mark.parametrize(
    "payload",
    (
        {"type": "note", "title": "笔记", "amount": 12},
        {"type": "ledger", "title": "账目", "amount": 12, "start_time": "2030-01-01T09:00:00"},
        {"type": "task", "title": "待办", "ledger_category": "餐饮"},
    ),
)
def test_create_rejects_cross_type_fields_without_writing(
    db: Database,
    payload: dict[str, Any],
) -> None:
    """统一创建模型虽接收五类字段，规范化器必须拒绝跨类型混入。"""

    owner_id = "owner-cross-type-create"
    error = _assert_http_error(
        422,
        lambda: items_api.create_item(
            items_api.ItemCreate(**payload),
            owner_id=owner_id,
            db=db,
        ),
    )

    assert "Unsupported" in str(error.detail)
    assert db.count_items(owner_id) == 0
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0


def test_create_dispatches_all_five_types_and_audits_atomically(db: Database) -> None:
    """一个创建入口应覆盖五类规范化器，并为每次成功写入一条审计。"""

    owner_id = "owner-create-all-types"
    bodies = (
        items_api.ItemCreate(
            type="event",
            title="会议",
            start_time="2030-06-01T09:00:00",
            end_time="2030-06-01T10:00:00",
        ),
        items_api.ItemCreate(type="task", title="提交", plan_date="2030-06-01"),
        items_api.ItemCreate(type="note", title="记录"),
        items_api.ItemCreate(type="diary", content="今天很好", diary_date="2030-06-01"),
        items_api.ItemCreate(type="ledger", title="午饭", amount=18.5),
    )

    created_types: list[str] = []
    for body in bodies:
        response = items_api.create_item(body, owner_id=owner_id, db=db)
        data = cast(dict[str, Any], response["data"])
        item = db.get_item(str(data["id"]), owner_id=owner_id)
        assert item is not None
        assert is_canonical_internal_id(data["id"])
        assert data["display_id"] == str(data["id"])[:8]
        created_types.append(get_item_type_value(item.type, default=""))

    audit_rows = (
        db.get_connection()
        .execute(
            "SELECT action, item_type FROM operation_logs WHERE user_id = ? ORDER BY id",
            (owner_id,),
        )
        .fetchall()
    )
    assert created_types == ["event", "task", "note", "diary", "ledger"]
    assert [(row["action"], row["item_type"]) for row in audit_rows] == [
        ("create", item_type) for item_type in created_types
    ]


def test_create_preserves_explicit_diary_time_and_resolves_note_references(db: Database) -> None:
    """显式日记时间不能被覆盖，笔记引用必须在创建时生成所有者范围快照。"""

    owner_id = "owner-create-specials"
    target_id = _insert_item(db, owner_id, "reference-target", "task", title="被引用待办")

    diary_response = items_api.create_item(
        items_api.ItemCreate(
            type="diary",
            content="带明确记录时间",
            diary_date="2030-08-01",
            entry_time="2030-08-01T13:30:00+00:00",
        ),
        owner_id=owner_id,
        db=db,
    )
    note_response = items_api.create_item(
        items_api.ItemCreate(
            type="note",
            title="引用笔记",
            related_items=[target_id],
        ),
        owner_id=owner_id,
        db=db,
    )

    diary_id = str(cast(dict[str, Any], diary_response["data"])["id"])
    note_id = str(cast(dict[str, Any], note_response["data"])["id"])
    diary = db.get_item(diary_id, owner_id=owner_id)
    note = db.get_item(note_id, owner_id=owner_id)
    assert diary is not None and diary.entry_time == "2030-08-01T13:30:00+00:00"
    assert diary.title == "2030-08-01 21:30 日记"
    assert note is not None and note.related_items == [target_id]
    assert note.references == [
        {
            "kind": "item",
            "id": target_id,
            "type": "task",
            "title": "被引用待办",
        }
    ]


def test_create_defensively_rejects_a_missing_resolved_type(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使类型解析器异常降级为空，也不能写入无类型记录。"""

    monkeypatch.setattr(items_api, "normalize_item_type_query", lambda _value: None)

    error = _assert_http_error(
        422,
        lambda: items_api.create_item(
            items_api.ItemCreate(type="note", title="不会创建"),
            owner_id="owner-missing-type",
            db=db,
        ),
    )
    assert error.detail == "Item type is required"


def test_update_rejects_empty_version_only_cross_type_and_stale_requests(db: Database) -> None:
    """无业务字段、跨类型字段和陈旧版本都不得产生写入或审计。"""

    owner_id = "owner-update-guards"
    _insert_item(db, owner_id, "guarded-note", "note", title="原始标题")

    _assert_http_error(
        422,
        lambda: items_api.update_item(
            "guarded-note", items_api.ItemUpdate(), owner_id=owner_id, db=db
        ),
    )
    _assert_http_error(
        422,
        lambda: items_api.update_item(
            "guarded-note", items_api.ItemUpdate(version=0), owner_id=owner_id, db=db
        ),
    )
    _assert_http_error(
        422,
        lambda: items_api.update_item(
            "guarded-note", items_api.ItemUpdate(amount=12), owner_id=owner_id, db=db
        ),
    )
    _assert_http_error(
        409,
        lambda: items_api.update_item(
            "guarded-note",
            items_api.ItemUpdate(title="新标题", version=99),
            owner_id=owner_id,
            db=db,
        ),
    )

    stored = db.get_item("guarded-note", owner_id=owner_id)
    assert stored is not None and stored.version == 0 and stored.title == "原始标题"
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0


def test_same_value_update_is_a_noop_without_version_or_audit_change(db: Database) -> None:
    """规范化后无变化的请求应返回当前版本，不能伪造一次更新。"""

    owner_id = "owner-update-noop"
    _insert_item(db, owner_id, "same-note", "note", title="相同标题")

    response = items_api.update_item(
        "same-note",
        items_api.ItemUpdate(title="相同标题", version=0),
        owner_id=owner_id,
        db=db,
    )

    assert response["message"] == "无变化"
    assert cast(dict[str, Any], response["data"])["version"] == 0
    stored = db.get_item("same-note", owner_id=owner_id)
    assert stored is not None and stored.version == 0
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0


def test_ledger_update_accepts_canonical_cents_and_rejects_invalid_note_title(
    db: Database,
) -> None:
    """同时提供金额镜像时以整数分为准，规范化错误统一转换为 422。"""

    owner_id = "owner-update-normalization"
    _insert_item(db, owner_id, "cent-ledger", "ledger", amount=1)
    _insert_item(db, owner_id, "invalid-title-note", "note", title="有效标题")

    response = items_api.update_item(
        "cent-ledger",
        items_api.ItemUpdate(amount=2, amount_cents=345),
        owner_id=owner_id,
        db=db,
    )
    assert response["message"] == "更新成功"
    ledger = db.get_item("cent-ledger", owner_id=owner_id)
    assert ledger is not None
    assert ledger.amount_cents == 345
    assert ledger.amount == 3.45

    _assert_http_error(
        422,
        lambda: items_api.update_item(
            "invalid-title-note",
            items_api.ItemUpdate(title=""),
            owner_id=owner_id,
            db=db,
        ),
    )


def test_blank_choice_filter_is_ignored(db: Database) -> None:
    """仅含空白的枚举查询等同未提供，不应错误推断条目类型。"""

    response = items_api.list_items(
        status="   ",
        transaction_type=" ",
        owner_id="owner-blank-filter",
        db=db,
    )
    assert cast(dict[str, Any], response["data"])["total"] == 0


def test_update_returns_404_and_reports_corrupt_stored_type(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不存在条目与损坏类型分别使用 404 和 500，避免误导调用方。"""

    _assert_http_error(
        404,
        lambda: items_api.update_item(
            "missing",
            items_api.ItemUpdate(title="新标题"),
            owner_id="owner-update-errors",
            db=db,
        ),
    )

    class CorruptItem:
        type = "legacy"

    monkeypatch.setattr(db, "get_item", lambda *_args, **_kwargs: CorruptItem())
    error = _assert_http_error(
        500,
        lambda: items_api.update_item(
            "corrupt",
            items_api.ItemUpdate(title="新标题"),
            owner_id="owner-update-errors",
            db=db,
        ),
    )
    assert error.detail == "Stored item has an unsupported type"


def test_minimal_event_update_writes_only_the_requested_field(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修改标题不能顺带重写备注、时间或提醒等未请求字段。"""

    owner_id = "owner-minimal-update"
    _insert_item(
        db,
        owner_id,
        "minimal-event",
        "event",
        title="旧标题",
        notes="必须保留",
        reminder_rules=[{"offset_seconds": 600}],
    )
    original_update = db.update_item
    captured_updates: dict[str, Any] = {}

    def capture_update(item_id: str, updates: Mapping[str, Any], **kwargs: Any) -> bool:
        captured_updates.update(updates)
        return original_update(item_id, dict(updates), **kwargs)

    monkeypatch.setattr(db, "update_item", capture_update)

    response = items_api.update_item(
        "minimal-event",
        items_api.ItemUpdate(title="新标题"),
        owner_id=owner_id,
        db=db,
    )

    assert response["message"] == "更新成功"
    assert captured_updates == {"title": "新标题"}
    stored = db.get_item("minimal-event", owner_id=owner_id)
    assert stored is not None and stored.title == "新标题" and stored.notes == "必须保留"


def test_explicit_null_clears_event_reminders(db: Database) -> None:
    """显式 null 与未提供字段语义不同：前者必须清空两种提醒表示。"""

    owner_id = "owner-clear-reminders"
    _insert_item(
        db,
        owner_id,
        "clear-event",
        "event",
        reminder_rules=[{"offset_seconds": 900}],
    )

    response = items_api.update_item(
        "clear-event",
        items_api.ItemUpdate(reminder_rules=None),
        owner_id=owner_id,
        db=db,
    )

    assert response["message"] == "更新成功"
    stored = db.get_item("clear-event", owner_id=owner_id)
    assert stored is not None
    assert stored.reminder_rules == []
    assert stored.remind_times == []


def test_update_and_delete_map_concurrent_disappearance_to_409(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取后并发消失应返回冲突，而不是伪装成成功。"""

    owner_id = "owner-concurrent-item"
    _insert_item(db, owner_id, "concurrent-note", "note")

    monkeypatch.setattr(db, "update_item", lambda *_args, **_kwargs: False)
    _assert_http_error(
        409,
        lambda: items_api.update_item(
            "concurrent-note",
            items_api.ItemUpdate(title="不会落库"),
            owner_id=owner_id,
            db=db,
        ),
    )
    monkeypatch.setattr(db, "delete_item", lambda *_args, **_kwargs: False)
    _assert_http_error(
        409,
        lambda: items_api.delete_item("concurrent-note", owner_id=owner_id, db=db),
    )


def test_generic_event_delete_removes_empty_multi_node_collection(db: Database) -> None:
    """通用删除入口也必须维护多节点日程图，不能留下空集合头。"""

    owner_id = "owner-generic-event-delete"
    collection_id = db.create_event_collection(
        {
            "id": "generic-collection",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "两节点集合",
        },
        [
            (
                "event-leaf-a",
                _normalized_item(
                    owner_id,
                    "event",
                    title="第一节点",
                    start_time="2030-07-01T09:00:00",
                ),
            ),
            (
                "event-leaf-b",
                _normalized_item(
                    owner_id,
                    "event",
                    title="第二节点",
                    start_time="2030-07-02T09:00:00",
                ),
            ),
        ],
    )

    first = items_api.delete_item("event-leaf-a", owner_id=owner_id, db=db)
    assert first["message"] == "已删除"
    assert db.get_event_collection(collection_id, owner_id) is not None

    second = items_api.delete_item("event-leaf-b", owner_id=owner_id, db=db)
    assert second["message"] == "已删除"
    assert db.get_event_collection(collection_id, owner_id) is None
    deleted = (
        db.get_connection()
        .execute(
            "SELECT deleted FROM event_collections WHERE id = ?",
            (collection_id,),
        )
        .fetchone()
    )
    assert deleted is not None and deleted["deleted"] == 1


def test_get_item_returns_owner_scoped_payload_and_404(db: Database) -> None:
    """单条读取必须按所有者隔离，并为不存在条目返回 404。"""

    _insert_item(db, "owner-a", "scoped-note", "note", title="仅 A 可见")

    response = items_api.get_item("scoped-note", owner_id="owner-a", db=db)
    assert cast(dict[str, Any], response["data"])["title"] == "仅 A 可见"
    _assert_http_error(
        404,
        lambda: items_api.get_item("scoped-note", owner_id="owner-b", db=db),
    )
    _assert_http_error(
        404,
        lambda: items_api.delete_item("missing", owner_id="owner-a", db=db),
    )
