"""账本周报、月报与预算摘要。"""

from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.services.exporter import ExporterService
from plugins.pendo.web.api.items import aggregate_items, list_items
from tests.helpers.pendo_test_support import ROOT, read_scheduled_delivery


class TestPendoFinanceSummaries:
    @pytest.mark.parametrize(
        ("sender_name", "now_utc", "task_name", "marker_name", "dates", "period_args"),
        [
            pytest.param(
                "send_weekly_finance_summaries",
                (2030, 1, 6, 13, 0),
                "weekly_finance_summary",
                "last_weekly_finance_summary_date",
                ("2029-12-31", "2030-01-06", "2029-12-30", "2030-01-07"),
                ("2030-W01", "2030-01-06", "12/31 - 01/06", "📆 本周财务总结"),
                id="weekly-sunday-evening",
            ),
            pytest.param(
                "send_month_end_finance_summaries",
                (2030, 3, 31, 13, 0),
                "month_end_finance_summary",
                "last_month_end_finance_summary_date",
                ("2030-03-01", "2030-03-31", "2030-02-28", "2030-04-01"),
                ("2030-03", "2030-03-31", "2030/03/01 - 2030/03/31", "🧾 月底财务总结"),
                id="month-end-evening",
            ),
        ],
    )
    def test_finance_summary_sends_at_period_end_evening(
        self, db, monkeypatch, sender_name, now_utc, task_name, marker_name, dates, period_args
    ):
        from datetime import datetime

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.utils.settings_utils import parse_custom_settings

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(*now_utc, tzinfo=UTC)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        period_key, period_end, period_label, period_title = period_args
        titles = ("区间首日支出", "区间末日支出", "区间前一天支出", "区间后一天支出")
        for index, (title, ledger_date) in enumerate(zip(titles, dates, strict=True)):
            db.insert_item(
                {
                    "id"              : f"summary-item-{index}",
                    "owner_id"        : "1001",
                    "type"            : "ledger",
                    "title"           : title,
                    "amount_cents"    : 1234 + index,
                    "transaction_type": "expense",
                    "ledger_category" : "餐饮",
                    "ledger_date"     : ledger_date,
                }
            )
        db.update_user_settings("1001", {"timezone": "Asia/Shanghai"})

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)

        result = asyncio.run(
            getattr(scheduled_module, sender_name)(SimpleNamespace(send_action=send_action), db)
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        text = actions[0]["params"]["message"][0]["data"]["text"]
        assert period_title in text
        assert period_label in text
        # 区间首日、末日两笔计入（12.34 + 12.35），区间前后一天的两笔不计入。
        assert "共 2 笔流水" in text
        assert "支出: ¥24.69" in text
        assert parse_custom_settings(db.get_user_settings("1001"))[marker_name] == period_end
        outbox = read_scheduled_delivery(db, task_name, "1001", period_key)
        assert outbox is not None
        assert outbox["state"] == "sent"

    def test_finance_summary_uses_amount_cents_and_ledger_date_range(self):
        import shutil
        import uuid

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.services.db import Database

        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_finance_summary_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        db       = Database(str(temp_dir / "pendo.db"))
        owner_id = "u-finance-summary"

        try:
            db.insert_item(
                {
                    "id"              : "sum_expense",
                    "owner_id"        : owner_id,
                    "type"            : "ledger",
                    "title"           : "午饭",
                    "amount_cents"    : 12345,
                    "transaction_type": "expense",
                    "ledger_category" : "餐饮",
                    "ledger_date"     : "2026-05-02",
                    "account_name"    : "微信",
                }
            )
            db.insert_item(
                {
                    "id"              : "sum_income",
                    "owner_id"        : owner_id,
                    "type"            : "ledger",
                    "title"           : "工资",
                    "amount_cents"    : 500000,
                    "transaction_type": "income",
                    "ledger_category" : "工资",
                    "ledger_date"     : "2026-05-03",
                    "account_name"    : "招商银行卡",
                }
            )
            db.insert_item(
                {
                    "id"                  : "sum_transfer",
                    "owner_id"            : owner_id,
                    "type"                : "ledger",
                    "title"               : "转入储蓄",
                    "amount_cents"        : 20000,
                    "transaction_type"    : "transfer",
                    "ledger_category"     : "转账",
                    "ledger_date"         : "2026-05-04",
                    "account_name"        : "招商银行卡",
                    "counter_account_name": "储蓄卡",
                }
            )
            db.insert_item(
                {
                    "id"              : "sum_outside",
                    "owner_id"        : owner_id,
                    "type"            : "ledger",
                    "title"           : "范围外支出",
                    "amount_cents"    : 999999,
                    "transaction_type": "expense",
                    "ledger_category" : "测试",
                    "ledger_date"     : "2026-06-01",
                    "account_name"    : "微信",
                }
            )
            conn = db.get_connection()
            with conn:
                conn.execute(
                    "UPDATE items SET amount = 0 WHERE id IN (?, ?)",
                    ("sum_expense", "sum_income"),
                )
            db.cache_clear()

            summary = asyncio.run(
                scheduled_module._generate_finance_summary_content(
                    db,
                    owner_id,
                    scheduled_module._FinancePeriod(
                        "2026-05",
                        "2026-05-01",
                        "2026-05-31",
                        "2026/05/01 - 2026/05/31",
                        "测试财务总结",
                    ),
                )
            )

            assert "🧾 共 3 笔流水" in summary
            assert "💰 收入: ¥5000.00" in summary
            assert "💸 支出: ¥123.45" in summary
            assert "📊 结余: +¥4876.55" in summary
            assert "🔁 转账: ¥200.00" in summary
            assert "📂 最大支出分类: 餐饮 ¥123.45" in summary
            assert "📥 主要收入来源: 工资 ¥5000.00" in summary
            assert "🔥 最大单笔支出: 午饭 ¥123.45 (2026-05-02)" in summary
            assert "账户收支:" in summary
            assert "招商银行卡 收入¥5000.00 支出¥0.00 净额+¥5000.00" in summary
            assert "微信 收入¥0.00 支出¥123.45 净额¥-123.45" in summary
            assert "转账流向:" in summary
            assert "招商银行卡 → 储蓄卡 ¥200.00" in summary
            assert "范围外支出" not in summary
        finally:
            db.cleanup()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_finance_metrics_ignore_non_ledger_models(self):

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import Item, ItemType, LedgerItem

        metrics = scheduled_module._summarize_finance_items(
            [
                Item(type=ItemType.LEDGER, title="错误模型"),
                LedgerItem(
                    title            = "有效支出",
                    amount_cents     = 1250,
                    transaction_type = "expense",
                ),
            ]
        )

        assert metrics.item_count == 1
        assert metrics.total_expense == 12.5
        assert metrics.top_expense is not None
        assert metrics.top_expense.title == "有效支出"

    def test_scheduled_private_send_skips_non_numeric_owner_ids(self):

        from plugins.pendo.commands import scheduled as scheduled_module

        messages = []
        result   = asyncio.run(
            scheduled_module._send_private_or_collect(
                SimpleNamespace(),
                messages,
                "demo_web_TEST",
                "测试消息",
            )
        )

        assert result is False
        assert messages == []


def test_currency_aggregation_keeps_independent_totals(tmp_path):
    db = Database(str(tmp_path / "currency.db"))
    try:
        for code, amount in [("CNY", 100), ("USD", 230)]:
            db.insert_item(
                {
                    "owner_id"        : "review",
                    "type"            : "ledger",
                    "title"           : code,
                    "amount"          : amount,
                    "currency"        : code,
                    "transaction_type": "expense",
                    "ledger_date"     : "2030-01-01",
                }
            )
        data = aggregate_items(owner_id="review", db=db)["data"]
        assert data["expense"] == 100
        assert data["by_currency"]["CNY"]["expense"] == 100
        assert data["by_currency"]["USD"]["expense"] == 230
        assert aggregate_items(owner_id="review", db=db, currency="usd")["data"]["expense"] == 230
        page = list_items(type="ledger", currency="USD", owner_id="review", db=db)["data"]
        assert page["total"] == 1
        assert page["items"][0]["currency"] == "USD"
        assert db.aggregate_ledger_amounts_by_day("review", {"type": "ledger"}, currency="USD")[
            "2030-01-01"
        ] == (23000, 0)
    finally:
        db.close_all_connections()


@pytest.mark.asyncio
async def test_currency_statistics_keep_independent_amounts(db, tmp_path):
    from plugins.pendo.commands.scheduled import (
        _FinancePeriod,
        _format_finance_summary,
        _summarize_finance_items,
    )
    from plugins.pendo.handlers.ledger import LedgerHandler

    for code in ["CNY", "USD"]:
        db.insert_item(
            {
                "owner_id"        : "u",
                "type"            : "ledger",
                "title"           : code,
                "currency"        : code,
                "amount_cents"    : 10000,
                "transaction_type": "expense",
                "ledger_date"     : "2030-01-01",
                "ledger_category" : "餐饮",
            }
        )
    items   = db.get_items("u")
    metrics = _summarize_finance_items(items)
    assert metrics.total_expense == 0
    assert {code: value.total_expense for code, value in metrics.by_currency.items()} == {
        "CNY": 100,
        "USD": 100,
    }
    period = _FinancePeriod("2030-01", "2030-01-01", "2030-01-31", "January", "Report")
    report = _format_finance_summary(metrics, period)
    assert "¥100.00" in report and "USD 100.00" in report and ("200.00" not in report)
    handler = LedgerHandler(db)
    for result in [
        await handler.summary("u", "2030-01", None),
        await handler.list_ledger("u", "2030-01 all", None),
    ]:
        assert result["status"] == "success"
        assert "¥100.00" in result["message"] and "USD 100.00" in result["message"]
        assert "200.00" not in result["message"]
    result = ExporterService(db, tmp_path).export_markdown("u", "ledger 2030-01 ledger", {})
    assert "USD 100.00" in Path(result["file_path"]).read_text(encoding="utf-8")
