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
        payload = {"amount_cents": 9947, **fields}
        result  = await LedgerHandler(db).handle(
            "u1", "record " + json.dumps(payload), SimpleNamespace(request_texts=(source,))
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
        payload = {"amount_cents": 9947, "title": "会员", **overrides}
        result  = await LedgerHandler(db).handle(
            "u1",
            "record " + json.dumps(payload),
            SimpleNamespace(request_texts=("记账99.47会员，服务，信用卡",)),
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
        delegated = SimpleNamespace(request_texts=("按上次同样的分类和账户记一笔打车48.5",))
        denied = await handler.handle("u2", "record " + json.dumps(payload), delegated)
        assert denied["status"] == "error" and db.get_all_items("u2") == []
        saved = await handler.handle("u1", "record " + json.dumps(payload), delegated)
        assert saved["status"] == "success"
        item = next(item for item in db.get_all_items("u1") if item.id == saved["item_id"])
        assert (item.amount_cents, item.ledger_category, item.account_name) == (
            4850,
            "出差",
            "公务卡",
        )
    finally:
        db.cleanup()


def test_manifest_structured_input_matches_ledger_record_model():
    """plugin.json 里给调用方的参数格式就是 LedgerRecord 本身（去掉只由 Core 传入的原话）。"""

    from pathlib import Path

    from plugins.pendo.models.ledger_record import LedgerRecord

    manifest = json.loads(
        (Path(__file__).parents[3] / "plugins/pendo/plugin.json").read_text(encoding="utf-8")
    )
    ledger   = next(n for n in manifest["commands"][0]["subcommands"] if n["name"] == "ledger")
    record   = next(n for n in ledger["subcommands"] if n["name"] == "record")
    expected = LedgerRecord.model_json_schema()
    expected["properties"].pop("source_text")
    expected.pop("title")
    assert record["effect"] == "write"
    assert record["structured_input"]["name"] == "pendo_ledger_add"
    assert record["structured_input"]["parameters"] == expected


@pytest.mark.asyncio
async def test_delegated_record_checks_fields_against_initiator_words_not_arguments(tmp_path):
    """代为执行时以 Core 传入的原话为准：参数里自带的 source_text 不能替分类作证。"""

    db = Database(str(tmp_path / "ledger.db"))
    try:
        context = SimpleNamespace(request_texts=("记账 99.47 会员",))
        forged = {
            "amount_cents": 9947,
            "title"       : "会员",
            "category"    : "软件",
            "source_text" : "会员 软件",
        }
        result = await LedgerHandler(db).handle("u1", "record " + json.dumps(forged), context)
        assert result["status"] == "error" and "软件" in result["message"]
        assert db.get_all_items("u1") == []

        grounded = {"amount_cents": 9947, "title": "会员"}
        result   = await LedgerHandler(db).handle("u1", "record " + json.dumps(grounded), context)
        assert result["status"] == "success"
        assert len(db.get_all_items("u1")) == 1
    finally:
        db.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["add", "quick"])
async def test_delegated_quick_ledger_is_grounded_like_record(tmp_path, verb):
    """代为执行的快捷记账和结构化记账同一套核对：原话里没有的账户不落库。"""

    db = Database(str(tmp_path / "ledger.db"))
    try:
        context = SimpleNamespace(request_texts=("记账 32 打车 交通",))
        refused = await LedgerHandler(db).handle(
            "u1", f"{verb} 32 打车 cat:交通 account:支付宝", context
        )
        assert refused["status"] == "error" and "支付宝" in refused["message"]
        saved = await LedgerHandler(db).handle("u1", f"{verb} 32 打车 cat:交通", context)
        assert saved["status"] == "success"
        items = db.get_all_items("u1")
        assert [(i.title, i.ledger_category, i.amount_cents) for i in items] == [
            ("打车", "交通", 3200)
        ]
    finally:
        db.cleanup()
