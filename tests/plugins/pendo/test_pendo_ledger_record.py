"""结构化记账在写入前校验类型、字段来源与描述边界。"""

import json
from types import SimpleNamespace

import pytest

from plugins.pendo.handlers.ledger import LedgerHandler
from plugins.pendo.services.db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,source,title,category,account",
    [
        (
            {"title": "稿费", "category": "兼职收入", "transaction_type": "income"},
            "稿费99.47算兼职收入，记一下",
            "稿费",
            "兼职",
            "现金",
        ),
        (
            {"title": "稿费", "category": "兼职收入", "transaction_type": "income"},
            "稿费99.47，分类叫“兼职收入”，记一下",
            "稿费",
            "兼职收入",
            "现金",
        ),
        (
            {"title": "会员 服务", "account": "信用卡"},
            "记账99.47 会员 服务 信用卡",
            "会员",
            "服务",
            "信用卡",
        ),
        (
            {"title": "DEEPSEEK 服务", "category": "服务", "account": "信用卡"},
            "记账 99.47 DEEPSEEK 服务 信用卡",
            "DEEPSEEK",
            "服务",
            "信用卡",
        ),
        (
            {"title": "楼下米线", "account": "信用卡"},
            "记账 99.47 楼下米线 信用卡",
            "楼下米线",
            "其他",
            "信用卡",
        ),
        (
            {"title": "售后 服务", "category": "服务"},
            "记账99.47，描述“售后 服务”，分类服务",
            "售后 服务",
            "服务",
            "现金",
        ),
        (
            {"title": "宠物 小黑保险", "category": "宠物"},
            "记账99.47 宠物 小黑保险",
            "小黑保险",
            "宠物",
            "现金",
        ),
    ],
)
async def test_record_saves_one_grounded_row(tmp_path, fields, source, title, category, account):
    db = Database(str(tmp_path / "ledger.db"))
    try:
        payload = {"amount_cents": 9947, **fields, "source_text": source}
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
        {"category": "软件"},
        {"account": "银行卡"},
        {"amount_cents": True},
        {"amount_cents": 99.47},
        {"amount_cents": -9947},
        {"unknown": "x"},
        {"transaction_type": "transfer"},
        {"transaction_type": "transfer", "to_account": "待定"},
    ],
)
async def test_invalid_record_never_writes(tmp_path, overrides):
    db = Database(str(tmp_path / "ledger.db"))
    try:
        payload = {
            "amount_cents": 9947,
            "title": "会员",
            "source_text": "记账99.47会员，服务，信用卡",
            **overrides,
        }
        result = await LedgerHandler(db).handle(
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
            "amount_cents": 4850,
            "title": "打车",
            "category_from": first["item_id"],
            "account_from": first["item_id"],
            "source_text": "按上次同样的分类和账户记一笔打车48.5",
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
