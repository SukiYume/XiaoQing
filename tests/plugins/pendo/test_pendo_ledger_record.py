"""结构化记账在写入前只校验字段本身；Agent 发来的命令和用户自己发的一样保存。"""

import json
from types import SimpleNamespace

import pytest

from plugins.pendo.handlers.ledger import LedgerHandler
from plugins.pendo.services.db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,title,category,account",
    [
        # “兼职收入”这类写法归到已有分类，不另建一个。
        (
            {"title": "稿费", "category": "兼职收入", "transaction_type": "income"},
            "稿费",
            "兼职",
            "现金",
        ),
        ({"title": "楼下米线", "account": "信用卡"}, "楼下米线", "其他", "信用卡"),
        ({"title": "小黑保险", "category": "宠物"}, "小黑保险", "宠物", "现金"),
        # 自定义分类和账户照常保存。
        ({"title": "会员", "category": "软件", "account": "银行卡"}, "会员", "软件", "银行卡"),
    ],
)
async def test_record_saves_the_fields_it_is_given(tmp_path, fields, title, category, account):
    db = Database(str(tmp_path / "ledger.db"))
    try:
        payload = {"amount_cents": 9947, **fields}
        result  = await LedgerHandler(db).handle(
            "u1", "record " + json.dumps(payload), SimpleNamespace()
        )
        assert result["status"] == "success"
        items = db.get_all_items("u1")
        assert len(items) == 1
        assert (items[0].title, items[0].ledger_category, items[0].account_name) == (
            title,
            category,
            account,
        )
        assert items[0].amount_cents == 9947
    finally:
        db.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_cents": True},
        {"amount_cents": 99.47},
        {"amount_cents": -9947},
        {"unknown": "x"},
        {"transaction_type": "transfer"},
        {"category": "餐饮", "category_from": "abcd1234"},
    ],
)
async def test_invalid_record_never_writes(tmp_path, overrides):
    db = Database(str(tmp_path / "ledger.db"))
    try:
        payload = {"amount_cents": 9947, "title": "会员", **overrides}
        result  = await LedgerHandler(db).handle(
            "u1", "record " + json.dumps(payload), SimpleNamespace()
        )
        assert result["status"] == "error"
        assert db.get_all_items("u1") == []
    finally:
        db.cleanup()


@pytest.mark.asyncio
async def test_record_inherits_verified_history_and_keeps_owner_boundary(tmp_path):
    db      = Database(str(tmp_path / "ledger.db"))
    handler = LedgerHandler(db)
    try:
        first = await handler.handle(
            "u1",
            'record {"amount_cents":1000,"title":"打车","category":"出差","account":"公务卡"}',
            SimpleNamespace(),
        )
        payload = {
            "amount_cents" : 4850,
            "title"        : "打车",
            "category_from": first["item_id"],
            "account_from" : first["item_id"],
        }
        denied = await handler.handle("u2", "record " + json.dumps(payload), SimpleNamespace())
        assert denied["status"] == "error" and db.get_all_items("u2") == []
        saved = await handler.handle("u1", "record " + json.dumps(payload), SimpleNamespace())
        assert saved["status"] == "success"
        item = next(item for item in db.get_all_items("u1") if item.id == saved["item_id"])
        assert (item.amount_cents, item.ledger_category, item.account_name) == (
            4850,
            "出差",
            "公务卡",
        )
    finally:
        db.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["add", "quick"])
async def test_agent_commands_are_saved_like_the_users_own(tmp_path, verb):
    """Agent 代为执行的快捷记账和用户自己发的同一条路：按命令里的字段保存，不再拿原话做字面比对。"""

    db = Database(str(tmp_path / "ledger.db"))
    try:
        delegated = SimpleNamespace(request_texts=("记账 159.7 很久以前 餐饮", "描述"))
        saved = await LedgerHandler(db).handle(
            "u1", f"{verb} 159.7 很久以前 cat:餐饮 account:银行卡", delegated
        )
        assert saved["status"] == "success"
        items = db.get_all_items("u1")
        assert [(i.title, i.ledger_category, i.account_name, i.amount_cents) for i in items] == [
            ("很久以前", "餐饮", "银行卡", 15970)
        ]
    finally:
        db.cleanup()
