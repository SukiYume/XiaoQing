"""日记、搜索和查看命令。"""

from __future__ import annotations

from tests.helpers.pendo_review_test_support import (
    Any,
    Database,
    NoteHandler,
    Path,
    SimpleNamespace,
    TaskHandler,
    _make_temp_db,
    asyncio,
    datetime,
    pytest,
    shutil,
    threading,
)


def test_diary_list_supports_cat_tag_and_rejects_invalid_range():
    from plugins.pendo.handlers.diary import DiaryHandler

    temp_dir, db = _make_temp_db("pendo_review_diary_list_filters")
    owner_id = "u-diary-list-filters"

    try:
        db.insert_item(
            {
                "id": "diary-a",
                "owner_id": owner_id,
                "type": "diary",
                "title": "CmdAudit 日记",
                "content": "CmdAudit 今天完成接口巡检",
                "category": "日记",
                "tags": ["cmdaudit", "复盘"],
                "diary_date": "2026-05-10",
                "entry_time": "2026-05-10T22:10:00",
                "mood": "happy",
                "created_at": "2026-05-10T22:10:00",
                "updated_at": "2026-05-10T22:10:00",
            }
        )

        handler = DiaryHandler(db)
        by_tag = asyncio.run(handler.list_diaries(owner_id, "2026-05 #cmdaudit", SimpleNamespace()))
        assert by_tag["status"] == "success"
        assert "diary-a" in by_tag["message"]

        by_cat = asyncio.run(handler.list_diaries(owner_id, "2026-05 cat:日记", SimpleNamespace()))
        assert by_cat["status"] == "success"
        assert "diary-a" in by_cat["message"]

        invalid = asyncio.run(handler.list_diaries(owner_id, "not-a-range", SimpleNamespace()))
        assert invalid["status"] == "error"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_search_supports_tag_filter_and_ledger_category_filter():
    from plugins.pendo.handlers.search import SearchHandler

    temp_dir, db = _make_temp_db("pendo_review_search_filters")
    owner_id = "u-search-filters"

    try:
        db.insert_item(
            {
                "id": "note-cmd",
                "owner_id": owner_id,
                "type": "note",
                "title": "CmdAudit RustDesk",
                "content": "CmdAudit note body",
                "category": "资料",
                "tags": ["cmdaudit"],
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "ledger-cmd",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "CmdAudit 超市采购",
                "content": "CmdAudit receipt",
                "category": "记账",
                "ledger_category": "餐饮",
                "transaction_type": "expense",
                "amount": 57,
                "amount_cents": 5700,
                "ledger_date": "2026-05-10",
                "created_at": "2026-05-10T09:00:00",
                "updated_at": "2026-05-10T09:00:00",
            }
        )

        handler = SearchHandler(db)

        by_tag = asyncio.run(handler.search(owner_id, "CmdAudit #cmdaudit", SimpleNamespace()))
        assert by_tag["status"] == "success"
        assert "note-cmd" in by_tag["message"]
        assert "ledger-cmd" not in by_tag["message"]

        by_ledger_category = asyncio.run(
            handler.search(
                owner_id, "CmdAudit type=ledger category=餐饮 range=2026-05", SimpleNamespace()
            )
        )
        assert by_ledger_category["status"] == "success"
        assert "ledger-cmd" in by_ledger_category["message"]

        by_ledger_start_day = asyncio.run(
            handler.search(
                owner_id,
                "CmdAudit type=ledger range=2026-05-10..2026-05-10",
                SimpleNamespace(),
            )
        )
        assert by_ledger_start_day["status"] == "success"
        assert "ledger-cmd" in by_ledger_start_day["message"]

        bad_type = asyncio.run(handler.search(owner_id, "CmdAudit type=bad", SimpleNamespace()))
        assert bad_type["status"] == "error"

        bad_range = asyncio.run(
            handler.search(owner_id, "CmdAudit range=not-a-range", SimpleNamespace())
        )
        assert bad_range["status"] == "error"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_search_parser_supports_quotes_type_inference_and_user_clock():
    from plugins.pendo.handlers.search import SearchHandler

    user_now = datetime(2032, 1, 15, 12, 0, 0)
    query, filters = SearchHandler._parse_search_query(
        '"annual report" category="工作 空间" #Focus',
        user_now,
    )
    assert query == "annual report"
    assert filters == {"category": "工作 空间", "tags": "Focus"}

    query, ledger_filters = SearchHandler._parse_search_query(
        '账单 account="Main Wallet" transaction_type=EXPENSE range=month',
        user_now,
    )
    assert query == "账单"
    assert ledger_filters["type"] == "ledger"
    assert ledger_filters["account_name"] == "Main Wallet"
    assert ledger_filters["transaction_type"] == "expense"
    assert ledger_filters["date_field"] == "ledger_date"
    assert ledger_filters["start_date"] == "2032-01-01"
    assert ledger_filters["end_date"] == "2032-01-31"


@pytest.mark.parametrize(
    "args",
    [
        "关键词 tag=one #two",
        "关键词 tags=one tag=two",
        "关键词 type=event type=task",
        "关键词 status=open account=现金",
        "关键词 type=note status=open",
        "关键词 type=task transaction_type=expense",
        "关键词 type=bad",
        "关键词 status=bad",
        "关键词 range=not-a-range",
        '关键词 category="未闭合',
        "关键词 #bad!",
        "关键词 type=",
        "关键词 tag=" + "x" * 21,
        "***",
        "x" * 101,
        "关键词 " + chr(1),
        "x" * 2_001,
    ],
)
def test_search_parser_rejects_ambiguous_or_unsafe_input(args: str) -> None:
    from plugins.pendo.handlers.search import SearchHandler

    with pytest.raises(ValueError):
        SearchHandler._parse_search_query(args, datetime(2032, 1, 15, 12, 0, 0))


def test_search_uses_paged_query_and_batches_event_collection_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.pendo.handlers.search import SearchHandler
    from plugins.pendo.models.item import EventItem

    main_thread = threading.get_ident()
    calls: dict[str, Any] = {}
    event = EventItem(
        id="search-event",
        owner_id="search-owner",
        title="子节点\n标题",
        content="needle content",
        event_collection_id="search-collection",
        start_time="2032-01-15T09:00:00",
    )

    class _ItemsRepo:
        def search_items_page(self, owner_id, query, filters, *, limit, offset):
            calls["search"] = (owner_id, query, filters, limit, offset, threading.get_ident())
            return [event], 22

        def get_event_collections_by_ids(self, owner_id, collection_ids):
            calls["collections"] = (owner_id, collection_ids, threading.get_ident())
            return {
                "search-collection": {
                    "id": "search-collection",
                    "title": "集合\n标题",
                }
            }

    monkeypatch.setattr(
        "plugins.pendo.utils.time_utils.now_in_timezone",
        lambda _user_id, _db: datetime(2032, 1, 15, 12, 0, 0),
    )
    result = asyncio.run(
        SearchHandler(_ItemsRepo()).search(
            "search-owner",
            "needle type=event",
            SimpleNamespace(),
        )
    )

    assert result["status"] == "success"
    assert calls["search"][:5] == (
        "search-owner",
        "needle",
        {"type": "event"},
        15,
        0,
    )
    assert calls["search"][5] != main_thread
    assert calls["collections"][:2] == ("search-owner", ["search-collection"])
    assert calls["collections"][2] != main_thread
    assert "命中: 22 条" in result["message"]
    assert "集合 标题 · 子节点 标题" in result["message"]
    assert "...还有 21 条结果" in result["message"]


def test_search_handler_reports_exact_total_and_limits_display(tmp_path: Path) -> None:
    from plugins.pendo.handlers.search import SearchHandler

    db = Database(str(tmp_path / "pendo-search-total.db"))
    owner_id = "search-total"
    try:
        for index in range(20):
            timestamp = f"2032-01-{index + 1:02d}T09:00:00"
            db.insert_item(
                {
                    "id": f"search-note-{index:02d}",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": f"Needle result {index}",
                    "content": "Needle body",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )

        result = asyncio.run(
            SearchHandler(db).search(owner_id, "Needle type=note", SimpleNamespace())
        )
        assert result["status"] == "success"
        assert "命中: 20 条" in result["message"]
        assert result["message"].count("ID: `") == 15
        assert "...还有 5 条结果" in result["message"]
    finally:
        db.cleanup()


def test_search_ledger_display_uses_integer_cents_and_remark_preview():
    from zoneinfo import ZoneInfo

    from plugins.pendo.handlers.search import SearchHandler
    from plugins.pendo.models.item import LedgerItem

    item = LedgerItem(
        id="search-ledger",
        owner_id="search-owner",
        title="超市\n采购",
        content="",
        amount=999.99,
        amount_cents=1234,
        ledger_date="2032-01-15",
        ledger_category="餐饮",
        account_name="现金",
        merchant="商店",
        remark="needle\nremark",
    )
    line = SearchHandler(SimpleNamespace())._format_item_line(
        item,
        "needle",
        display_timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert "超市 采购" in line
    assert "-¥12.34" in line
    assert "needle remark" in line


def test_view_commands_reject_extra_arguments():
    from plugins.pendo.handlers.diary import DiaryHandler
    from plugins.pendo.handlers.event import EventHandler
    from plugins.pendo.handlers.ledger import LedgerHandler
    from plugins.pendo.models.item import TaskStatus

    temp_dir, db = _make_temp_db("pendo_review_view_args")
    owner_id = "u-view-args"

    try:
        db.insert_item(
            {
                "id": "view-event",
                "owner_id": owner_id,
                "type": "event",
                "title": "CmdAudit 日程",
                "start_time": "2026-05-10T09:00:00",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "view-task",
                "owner_id": owner_id,
                "type": "task",
                "title": "CmdAudit 待办",
                "status": TaskStatus.OPEN.value,
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "view-note",
                "owner_id": owner_id,
                "type": "note",
                "title": "CmdAudit 笔记",
                "content": "正文",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "view-ledger",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "CmdAudit 账目",
                "amount": 57,
                "amount_cents": 5700,
                "ledger_category": "餐饮",
                "ledger_date": "2026-05-10",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "view-diary",
                "owner_id": owner_id,
                "type": "diary",
                "title": "CmdAudit 日记",
                "content": "正文",
                "diary_date": "2026-05-10",
                "entry_time": "2026-05-10T22:10:00",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        checks = [
            EventHandler(db, SimpleNamespace(), SimpleNamespace()).view_event(
                owner_id, "view-event extra", SimpleNamespace()
            ),
            TaskHandler(db).view_task(owner_id, "view-task extra", SimpleNamespace()),
            NoteHandler(db).view_note(owner_id, "view-note extra", SimpleNamespace()),
            LedgerHandler(db).view_ledger(owner_id, "view-ledger extra", SimpleNamespace()),
            DiaryHandler(db).view_diary(owner_id, "view-diary extra", SimpleNamespace()),
        ]

        for coro in checks:
            result = asyncio.run(coro)
            assert result["status"] == "error"
            assert "只接受" in result["message"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)
