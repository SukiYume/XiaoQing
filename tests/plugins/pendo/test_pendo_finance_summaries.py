"""账本周报、月报与预算摘要。"""

from __future__ import annotations

from datetime import UTC

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    _single_user_shanghai_settings,
    _with_scheduled_delivery_contract,
    asyncio,
)


class TestPendoFinanceSummaries:
    def test_weekly_finance_summary_sends_on_sunday_evening(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 6, 13, 0, tzinfo=UTC)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
            return "weekly-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", _single_user_shanghai_settings
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db     = _with_scheduled_delivery_contract(SimpleNamespace())
        result = asyncio.run(
            scheduled_module.send_weekly_finance_summaries(
                SimpleNamespace(send_action=send_action), db
            )
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "weekly-summary" in actions[0]["params"]["message"][0]["data"]["text"]
        assert generate_calls == [
            (
                db,
                "1001",
                scheduled_module._FinancePeriod(
                    "2030-W01",
                    "2029-12-31",
                    "2030-01-06",
                    "12/31 - 01/06",
                    "📆 本周财务总结",
                ),
            )
        ]

    def test_month_end_finance_summary_sends_on_last_day_evening(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 3, 31, 13, 0, tzinfo=UTC)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
            return "month-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", _single_user_shanghai_settings
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db     = _with_scheduled_delivery_contract(SimpleNamespace())
        result = asyncio.run(
            scheduled_module.send_month_end_finance_summaries(
                SimpleNamespace(send_action=send_action), db
            )
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "month-summary" in actions[0]["params"]["message"][0]["data"]["text"]
        assert generate_calls == [
            (
                db,
                "1001",
                scheduled_module._FinancePeriod(
                    "2030-03",
                    "2030-03-01",
                    "2030-03-31",
                    "2030/03/01 - 2030/03/31",
                    "🧾 月底财务总结",
                ),
            )
        ]

    def test_finance_summary_uses_amount_cents_and_ledger_date_range(self):
        import shutil
        import sys
        import uuid

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.services.db import Database

        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_finance_summary_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        db       = Database(str(temp_dir / "pendo.db"))
        owner_id = "u-finance-summary"

        try:
            db.insert_item(
                {
                    "id": "sum_expense",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "午饭",
                    "amount_cents": 12345,
                    "transaction_type": "expense",
                    "ledger_category": "餐饮",
                    "ledger_date": "2026-05-02",
                    "account_name": "微信",
                }
            )
            db.insert_item(
                {
                    "id": "sum_income",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "工资",
                    "amount_cents": 500000,
                    "transaction_type": "income",
                    "ledger_category": "工资",
                    "ledger_date": "2026-05-03",
                    "account_name": "招商银行卡",
                }
            )
            db.insert_item(
                {
                    "id": "sum_transfer",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "转入储蓄",
                    "amount_cents": 20000,
                    "transaction_type": "transfer",
                    "ledger_category": "转账",
                    "ledger_date": "2026-05-04",
                    "account_name": "招商银行卡",
                    "counter_account_name": "储蓄卡",
                }
            )
            db.insert_item(
                {
                    "id": "sum_outside",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "范围外支出",
                    "amount_cents": 999999,
                    "transaction_type": "expense",
                    "ledger_category": "测试",
                    "ledger_date": "2026-06-01",
                    "account_name": "微信",
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
        import sys

        sys.path.insert(0, str(ROOT))

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
        import sys

        sys.path.insert(0, str(ROOT))

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
