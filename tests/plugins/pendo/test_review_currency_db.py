"""货币回归：按币种过滤和聚合，保留分页计数与金额单位的一致性。"""

from plugins.pendo.services.db import Database
from plugins.pendo.web.api.items import aggregate_items, list_items


def test_currency_aggregation_keeps_independent_totals(tmp_path):
    db = Database(str(tmp_path / "currency.db"))
    try:
        for code in ("CNY", "USD"):
            db.insert_item(
                {
                    "owner_id": "review",
                    "type": "ledger",
                    "title": code,
                    "amount": 100,
                    "currency": code,
                    "transaction_type": "expense",
                    "ledger_date": "2030-01-01",
                }
            )
        data = aggregate_items(owner_id="review", db=db)["data"]
        assert data["expense"] == 100
        assert data["by_currency"]["CNY"]["expense"] == 100
        assert data["by_currency"]["USD"]["expense"] == 100
        assert aggregate_items(owner_id="review", db=db, currency="usd")["data"]["expense"] == 100
        page = list_items(type="ledger", currency="USD", owner_id="review", db=db)["data"]
        assert page["total"] == 1
        assert page["items"][0]["currency"] == "USD"
        assert db.aggregate_ledger_amounts_by_day("review", {"type": "ledger"}, currency="USD")[
            "2030-01-01"
        ] == (10000, 0)
    finally:
        db.close_all_connections()
