"""Pendo Web 条目字段和查询。"""

from __future__ import annotations

from tests.helpers.pendo_web_items_test_support import (
    ROOT,
    Database,
    items_api,
    normalize_event_fields,
    normalize_ledger_fields,
    normalize_note_fields,
    normalize_task_fields,
    pytest,
    shutil,
    uuid,
)


def test_items_api_and_database_use_the_ledger_category_column():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger"
    items_module = items_api

    try:
        db.insert_item(
            {
                "id": "l1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 22.5,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-25",
            }
        )
        db.insert_item(
            {
                "id": "l2",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "地铁",
                "amount": 4,
                "transaction_type": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-03-25",
            }
        )

        items = db.get_items(owner_id, filters={"type": "ledger", "ledger_category": "餐饮"})
        response = items_module.list_items(
            type="ledger",
            category=" 餐饮 ",
            owner_id=owner_id,
            db=db,
        )
        categories = items_module.list_categories(type="ledger", owner_id=owner_id, db=db)

        assert len(items) == 1
        assert items[0].ledger_category == "餐饮"
        assert items[0].title == "午饭"
        assert response["data"]["total"] == 1
        assert [item["id"] for item in response["data"]["items"]] == ["l1"]
        assert set(categories["data"]["categories"]) == {"交通", "餐饮"}
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_items_list_applies_priority_before_pagination_and_total_count():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_priority_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-priority"
    items_module = items_api

    try:
        db.insert_item(
            {
                "id": "task_nonmatch",
                "owner_id": owner_id,
                "type": "task",
                "title": "普通优先级",
                "priority": 3,
                "status": "open",
                "created_at": "2026-03-03T09:00:00",
                "updated_at": "2026-03-03T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task_match_1",
                "owner_id": owner_id,
                "type": "task",
                "title": "高优先级一",
                "priority": 1,
                "status": "open",
                "created_at": "2026-03-02T09:00:00",
                "updated_at": "2026-03-02T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task_match_2",
                "owner_id": owner_id,
                "type": "task",
                "title": "高优先级二",
                "priority": 1,
                "status": "open",
                "created_at": "2026-03-01T09:00:00",
                "updated_at": "2026-03-01T09:00:00",
            }
        )

        result = items_module.list_items(
            type="task",
            priority=1,
            page=1,
            page_size=1,
            owner_id=owner_id,
            db=db,
        )

        assert result["data"]["total"] == 2
        assert [item["id"] for item in result["data"]["items"]] == ["task_match_1"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_resolve_date_field_rejects_untrusted_field_names():
    items_module = items_api

    with pytest.raises(Exception) as exc_info:
        items_module._resolve_date_field("ledger", "created_at); DROP TABLE items; --")

    error = exc_info.value
    assert getattr(error, "status_code", None) == 422
    assert "Invalid date_field" in getattr(error, "detail", str(error))


def test_resolve_date_field_restricts_fields_by_item_type():
    items_module = items_api

    assert items_module._resolve_date_field("task", "plan_date") == "plan_date"
    assert items_module._resolve_date_field("task", "deadline_at") == "deadline_at"
    assert items_module._resolve_date_field("task", "created_at") == "created_at"

    with pytest.raises(Exception) as exc_info:
        items_module._resolve_date_field("task", "ledger_date")

    error = exc_info.value
    assert getattr(error, "status_code", None) == 422
    assert "task" in getattr(error, "detail", str(error))


def test_database_get_items_supports_diary_date_sort_field():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_diary_sort_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-sort"

    try:
        db.insert_item(
            {
                "id": "d2",
                "owner_id": owner_id,
                "type": "diary",
                "title": "后一天",
                "content": "第二篇",
                "diary_date": "2026-03-20",
                "entry_time": "2026-03-20T22:00:00",
                "created_at": "2026-03-18T20:00:00",
                "updated_at": "2026-03-18T20:00:00",
            }
        )
        db.insert_item(
            {
                "id": "d3",
                "owner_id": owner_id,
                "type": "diary",
                "title": "同一天早些",
                "content": "第三篇",
                "diary_date": "2026-03-20",
                "entry_time": "2026-03-20T08:00:00",
                "created_at": "2026-03-20T08:00:00",
                "updated_at": "2026-03-20T08:00:00",
            }
        )
        db.insert_item(
            {
                "id": "d1",
                "owner_id": owner_id,
                "type": "diary",
                "title": "前一天",
                "content": "第一篇",
                "diary_date": "2026-03-19",
                "entry_time": "2026-03-19T20:00:00",
                "created_at": "2026-03-21T20:00:00",
                "updated_at": "2026-03-21T20:00:00",
            }
        )

        items = db.get_items(
            owner_id,
            filters={"type": "diary", "sort_field": "diary_date", "sort_order": "ASC"},
            limit=10,
        )

        assert [item.id for item in items] == ["d1", "d2", "d3"]

        by_entry_time = db.get_items(
            owner_id,
            filters={"type": "diary", "sort_field": "entry_time", "sort_order": "DESC"},
            limit=10,
        )
        assert [item.id for item in by_entry_time] == ["d2", "d3", "d1"]

        items_module = items_api
        response = items_module.list_items(
            type="diary",
            sort="entry_time",
            order="desc",
            page=1,
            page_size=10,
            owner_id=owner_id,
            db=db,
        )
        assert [item["id"] for item in response["data"]["items"]] == ["d2", "d3", "d1"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_ledger_fields_sets_defaults_and_rejects_invalid_amount():
    result = normalize_ledger_fields({"title": "午饭", "amount": 18.5}, partial=False)

    assert result["amount"] == 18.5
    assert result["amount_cents"] == 1850
    assert result["transaction_type"] == "expense"
    assert result["currency"] == "CNY"
    assert result["account_name"] == "现金"
    assert result["ledger_category"] == "其他"
    assert result["ledger_date"]

    with pytest.raises(ValueError, match="greater than 0"):
        normalize_ledger_fields({"title": "坏数据", "amount": 0}, partial=False)


def test_update_ledger_item_recomputes_amount_cents_when_amount_changes():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_amount_update_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-amount"
    items_module = items_api

    try:
        db.insert_item(
            {
                "id": "ledger_amount",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 12.34,
                "amount_cents": 1234,
                "transaction_type": "expense",
                "currency": "CNY",
                "ledger_category": "餐饮",
                "ledger_date": "2026-04-29",
                "account_name": "现金",
                "created_at": "2026-04-29T12:00:00",
                "updated_at": "2026-04-29T12:00:00",
            }
        )

        response = items_module.update_item(
            "ledger_amount",
            body=items_module.ItemUpdate(amount=56.78),
            owner_id=owner_id,
            db=db,
        )
        item = db.get_item("ledger_amount", owner_id=owner_id)

        assert response["ok"] is True
        assert item.amount == 56.78
        assert item.amount_cents == 5678
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_row_to_item_does_not_mask_incomplete_ledger_rows():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_strict_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-strict"

    try:
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO items
                    (id, owner_id, type, title, amount, amount_cents, transaction_type,
                     currency, ledger_category, ledger_date, account_name, created_at, updated_at)
                VALUES
                    (?, ?, 'ledger', '脏账目', 12.0, 1200, NULL, NULL, '其他', '2026-03-25', NULL, ?, ?)
                """,
                (
                    "ledger_dirty",
                    owner_id,
                    "2026-03-25T12:00:00",
                    "2026-03-25T12:00:00",
                ),
            )

        item = db.get_item("ledger_dirty", owner_id=owner_id)

        assert item is not None
        assert item.transaction_type is None
        assert item.currency is None
        assert item.account_name is None
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_ledger_fields_handles_transfer_and_rejects_invalid_update_values():
    result = normalize_ledger_fields(
        {
            "title": "信用卡还款",
            "amount_cents": 120000,
            "transaction_type": "transfer",
            "account_name": "招行储蓄卡",
            "counter_account_name": "招行信用卡",
            "ledger_category": "",
        },
        partial=False,
    )

    assert result["amount"] == 1200
    assert result["transaction_type"] == "transfer"
    assert result["ledger_category"] == "转账"
    assert result["account_name"] == "招行储蓄卡"
    assert result["counter_account_name"] == "招行信用卡"

    with pytest.raises(ValueError, match="Invalid ledger transaction type"):
        normalize_ledger_fields({"transaction_type": "sideways"}, partial=True)

    with pytest.raises(ValueError, match="different"):
        normalize_ledger_fields(
            {
                "title": "坏转账",
                "amount": 10,
                "transaction_type": "transfer",
                "account_name": "微信",
                "counter_account_name": "微信",
            },
            partial=False,
        )

    with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
        normalize_ledger_fields({"ledger_date": "2026/03/25"}, partial=True)


def test_ledger_aggregate_tracks_transfer_separately_and_lists_accounts():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_v2_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-v2"
    items_module = items_api

    try:
        for record in [
            {
                "id": "expense_1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount_cents": 3250,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-18",
                "account_name": "微信",
                "merchant": "食堂",
            },
            {
                "id": "income_1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "工资",
                "amount_cents": 500000,
                "transaction_type": "income",
                "ledger_category": "工资",
                "ledger_date": "2026-03-18",
                "account_name": "招行",
                "merchant": "公司",
            },
            {
                "id": "transfer_1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "信用卡还款",
                "amount_cents": 120000,
                "transaction_type": "transfer",
                "ledger_category": "转账",
                "ledger_date": "2026-03-18",
                "account_name": "微信",
                "counter_account_name": "招行信用卡",
            },
        ]:
            db.insert_item(record)

        aggregate = items_module.aggregate_items(
            type="ledger",
            start_date="2026-03-01",
            end_date="2026-03-31",
            owner_id=owner_id,
            db=db,
        )["data"]
        transfer_account = items_module.aggregate_items(
            type="ledger",
            account_name="招行信用卡",
            start_date="2026-03-01",
            end_date="2026-03-31",
            owner_id=owner_id,
            db=db,
        )["data"]
        accounts = items_module.list_ledger_accounts(owner_id=owner_id, db=db)["data"]["accounts"]

        assert aggregate == {
            "income": 5000,
            "expense": 32.5,
            "transfer": 1200,
            "balance": 4967.5,
            "count": 3,
        }
        assert transfer_account == {
            "income": 0,
            "expense": 0,
            "transfer": 1200,
            "balance": 0,
            "count": 1,
        }
        assert accounts == ["微信", "招行", "招行信用卡"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_event_fields_normalizes_event_graph_fields_and_deduplicates_reminders():
    result = normalize_event_fields(
        {
            "title": "  发布准备  ",
            "category": "项目",
            "location": "  A1  ",
            "notes": "  备注  ",
            "start_time": "2026-03-13T18:00",
            "end_time": "2026-03-14T10:00",
            "event_role": "single",
            "remind_times": [
                "2026-03-13T17:00",
                "2026-03-13T17:00",
                "2026-03-14T09:00",
            ],
        },
        partial=False,
    )

    assert result["title"] == "发布准备"
    assert result["location"] == "A1"
    assert result["notes"] == "备注"
    assert result["start_time"] == "2026-03-13T18:00:00"
    assert result["end_time"] == "2026-03-14T10:00:00"
    assert result["event_role"] == "single"
    assert result["remind_times"] == ["2026-03-13T17:00:00", "2026-03-13T18:00:00"]

    with pytest.raises(ValueError, match="after start_time"):
        normalize_event_fields(
            {
                "title": "坏事件",
                "start_time": "2026-03-14T10:00",
                "end_time": "2026-03-13T10:00",
            },
            partial=False,
        )

    with pytest.raises(ValueError, match="Invalid event_role"):
        normalize_event_fields(
            {
                "title": "坏事件",
                "start_time": "2026-03-14T10:00",
                "event_role": "collection",
            },
            partial=False,
        )


def test_normalize_event_fields_compares_timezone_offsets_by_absolute_time():
    result = normalize_event_fields(
        {
            "title": "跨时区会议",
            "start_time": "2026-03-14T10:00:00+08:00",
            "end_time": "2026-03-14T03:30:00+00:00",
        },
        partial=False,
    )

    assert result["end_time"] == "2026-03-14T03:30:00+00:00"

    with pytest.raises(ValueError, match="after start_time"):
        normalize_event_fields(
            {
                "title": "绝对时间倒序",
                "start_time": "2026-03-14T10:00:00+00:00",
                "end_time": "2026-03-14T17:00:00+08:00",
            },
            partial=False,
        )

    with pytest.raises(ValueError, match="matching timezone forms"):
        normalize_event_fields(
            {
                "title": "时区形式混用",
                "start_time": "2026-03-14T10:00:00",
                "end_time": "2026-03-14T11:00:00+08:00",
            },
            partial=False,
        )


def test_event_update_route_preserves_event_notes_when_title_changes():
    temp_dir = (
        ROOT / ".pytest_cache" / "tmp" / f"pendo_event_update_preserves_notes_{uuid.uuid4().hex}"
    )
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-metadata"
    items_module = items_api

    try:
        db.insert_item(
            normalize_event_fields(
                {
                    "id": "ev-note",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "学术会议",
                    "category": "会议",
                    "start_time": "2026-04-22T12:43:00",
                    "end_time": "2026-04-26T12:00:00",
                    "remind_times": ["2026-04-21T12:43:00", "2026-04-25T12:00:00"],
                    "notes": "全局备注",
                },
                partial=False,
            )
        )

        body = items_module.ItemUpdate(
            title="学术会议（更新标题）",
        )

        result = items_module.update_item(
            "ev-note",
            body=body,
            owner_id=owner_id,
            db=db,
        )

        assert result["ok"] is True
        updated = db.get_item("ev-note", owner_id=owner_id)
        assert updated is not None
        assert updated.title == "学术会议（更新标题）"
        assert updated.notes == "全局备注"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_task_fields_accepts_priority_five_and_manages_completed_at():
    task = normalize_task_fields(
        {
            "title": "收尾任务",
            "category": "工作",
            "priority": 5,
            "status": "done",
            "plan_date": "2026-03-26",
            "deadline_at": "2026-03-26T18:00",
        },
        partial=False,
    )

    assert task["priority"] == 5
    assert task["status"] == "done"
    assert task["plan_date"] == "2026-03-26"
    assert task["deadline_at"] == "2026-03-26T18:00:00"
    assert task["completed_at"]

    reopened = normalize_task_fields(
        {
            **task,
            "status": "open",
        },
        partial=False,
    )
    assert reopened["completed_at"] is None

    with pytest.raises(ValueError, match="Invalid task status"):
        normalize_task_fields({"title": "坏任务", "status": "stuck"}, partial=False)

    with pytest.raises(ValueError, match="deadline_at"):
        normalize_task_fields({"title": "坏任务", "deadline_at": "tomorrow"}, partial=False)

    with pytest.raises(ValueError, match="legacy task field"):
        normalize_task_fields({"title": "坏任务", "due_time": "2026-03-26T18:00"}, partial=False)


def test_item_create_model_accepts_explicit_nullable_text_fields():
    items_module = items_api

    body = items_module.ItemCreate(type="note", title=None, content=None, category=None)

    assert body.title is None
    assert body.content is None
    assert body.category is None


def test_task_update_route_preserves_explicit_nulls_for_clearing_fields(tmp_path, request):
    items_module = items_api
    db = Database(str(tmp_path / "pendo_task_clear_fields.db"))
    request.addfinalizer(db.cleanup)
    owner_id = "u-task-clear-fields"
    db.insert_item(
        normalize_task_fields(
            {
                "id": "task-clear-fields",
                "owner_id": owner_id,
                "type": "task",
                "title": "清空字段",
                "content": "旧备注",
                "category": "工作",
                "status": "open",
                "priority": 2,
                "plan_date": "2026-03-26",
                "deadline_at": "2026-03-26T18:00:00",
            },
            partial=False,
        )
    )

    response = items_module.update_item(
        "task-clear-fields",
        items_module.ItemUpdate(
            deadline_at=None,
            plan_date=None,
            category=None,
            content=None,
        ),
        owner_id=owner_id,
        db=db,
    )
    updated = db.get_item("task-clear-fields", owner_id=owner_id)

    assert response["message"] == "更新成功"
    assert updated is not None
    assert updated.deadline_at is None
    assert updated.plan_date is None
    assert updated.category == "未分类"
    assert updated.content == ""


def test_normalize_note_fields_sets_defaults_and_deduplicates_tags():
    note = normalize_note_fields(
        {
            "title": "  读书摘录  ",
            "content": "  很长的正文  ",
            "category": "",
            "tags": ["学习", "学习", " 阅读 ", ""],
        },
        partial=False,
    )

    assert note["title"] == "读书摘录"
    assert note["content"] == "很长的正文"
    assert note["category"] == "未分类"
    assert note["tags"] == ["学习", "阅读"]


def test_normalize_note_fields_normalizes_references_and_related_items():
    note = normalize_note_fields(
        {
            "title": "引用笔记",
            "content": "正文",
            "references": [
                {"kind": "item", "id": " task_1 ", "type": "task", "title": " 待办标题 "},
                {"kind": "item", "id": "task_1", "type": "task", "title": "重复"},
                {"id": "event_1"},
                "bad",
            ],
            "related_items": ["event_1", "note_1", "note_1"],
        },
        partial=False,
    )

    assert note["references"] == [
        {"kind": "item", "id": "task_1", "type": "task", "title": "待办标题"},
        {"kind": "item", "id": "event_1"},
    ]
    assert note["related_items"] == ["event_1", "note_1", "task_1"]


def test_database_get_items_filters_note_tags_exactly_and_keyword():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_exact_tag_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-exact-tag"
    items_module = items_api

    try:
        db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_work",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": "工作笔记",
                    "content": "普通内容",
                    "tags": ["工作"],
                    "created_at": "2026-04-01T10:00:00",
                    "updated_at": "2026-04-01T10:00:00",
                },
                partial=False,
            )
        )
        db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_workflow",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": "流程笔记",
                    "content": "深度流程内容",
                    "tags": ["工作流"],
                    "created_at": "2026-04-02T10:00:00",
                    "updated_at": "2026-04-02T10:00:00",
                },
                partial=False,
            )
        )

        tagged = db.get_items(owner_id, filters={"type": "note", "tags": "工作"}, limit=10)
        keyword = db.get_items(owner_id, filters={"type": "note", "keyword": "深度"}, limit=10)
        api_tagged = items_module.list_items(
            type="note",
            tags=" 工作 ",
            owner_id=owner_id,
            db=db,
        )

        assert [item.id for item in tagged] == ["note_work"]
        assert [item.id for item in keyword] == ["note_workflow"]
        assert [item["id"] for item in api_tagged["data"]["items"]] == ["note_work"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_items_list_keyword_matches_extended_fields_and_total_count():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_keyword_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-items-keyword"
    items_module = items_api

    try:
        db.insert_item(
            {
                "id": "ledger_merchant",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 42,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-05-03",
                "account_name": "微信",
                "merchant": "窗口商户",
                "remark": "楼下小店",
                "created_at": "2026-05-03T10:00:00",
                "updated_at": "2026-05-03T10:00:00",
            }
        )
        db.insert_item(
            {
                "id": "event_location",
                "owner_id": owner_id,
                "type": "event",
                "title": "项目会",
                "category": "工作",
                "location": "南楼会议室",
                "notes": "季度复盘",
                "start_time": "2026-05-03T09:00:00",
                "end_time": "2026-05-03T10:00:00",
                "created_at": "2026-05-03T09:00:00",
                "updated_at": "2026-05-03T09:00:00",
            }
        )

        ledger_result = items_module.list_items(
            type="ledger",
            keyword="窗口商户",
            page=1,
            page_size=10,
            owner_id=owner_id,
            db=db,
        )
        event_result = items_module.list_items(
            type="event",
            keyword="会议室",
            page=1,
            page_size=10,
            owner_id=owner_id,
            db=db,
        )

        assert ledger_result["data"]["total"] == 1
        assert [item["id"] for item in ledger_result["data"]["items"]] == ["ledger_merchant"]
        assert event_result["data"]["total"] == 1
        assert [item["id"] for item in event_result["data"]["items"]] == ["event_location"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)
