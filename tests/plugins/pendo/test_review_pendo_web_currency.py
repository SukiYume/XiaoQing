"""Web 金额统计、图表和小组件按币种隔离的回归。"""

import json
import shutil
import subprocess
from datetime import date, datetime

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.dashboard_overview import build_dashboard_overview
from plugins.pendo.web.analytics.ledger_insights import build_ledger_insights
from plugins.pendo.web.api import stats, widget
from tests.helpers.paths import REPOSITORY_ROOT


@pytest.fixture
def currency_db(tmp_path):
    db = Database(str(tmp_path / "currency.db"))
    for currency, amount in [("CNY", 100), ("USD", 200)]:
        db.insert_item(
            {
                "id": currency,
                "owner_id": "owner",
                "type": "ledger",
                "title": currency,
                "currency": currency,
                "amount": amount,
                "amount_cents": amount * 100,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-01",
            }
        )
    yield db
    db.cleanup()


def test_insights_keep_currency_trends_separate(currency_db):
    result = build_ledger_insights(
        currency_db, "owner", start_date="2026-03-01", end_date="2026-03-02", currency="USD"
    )
    assert result["currency"] == "USD"
    assert result["summary"]["expense_total"] == 200
    assert sum(point["total"] for point in result["expense_timeline"]) == 200
    assert result["by_currency"]["CNY"]["expense"] == 100
    assert result["by_currency"]["USD"]["expense"] == 200


def test_stats_and_comparison_select_one_currency(currency_db, monkeypatch):
    monkeypatch.setattr(stats, "_today", lambda *_: date(2026, 3, 2))
    result = stats.ledger_stats(
        start_date = "2026-03-01",
        end_date   = "2026-03-02",
        currency   = "USD",
        owner_id   = "owner",
        db         = currency_db,
    )["data"]
    assert result["monthly"][0]["expense"] == 200
    assert result["expense_by_category"][0]["total"] == 200
    comparison = stats.ledger_comparison(
        months=3, currency="USD", owner_id="owner", db=currency_db
    )["data"]
    assert comparison["currency"] == "USD"
    assert comparison["months"][-1]["expense"] == 200


def test_dashboard_and_widget_show_currency_specific_totals(currency_db):
    now       = datetime(2026, 3, 2, 10)
    dashboard = build_dashboard_overview(currency_db, "owner", now)
    assert dashboard["month_summary"]["expense"] == 100
    assert dashboard["ledger_by_currency"]["USD"]["expense"] == 200
    panel = widget._build_ledger_panel(currency_db, "owner", now)
    assert panel["summary"]["primary"] == "支出 ¥100"
    assert "USD 支出 200" in panel["summary"]["secondary"]
    usd = next(item for item in panel["items"] if item["title"] == "USD")
    assert usd["amount_text"] == "-USD 200"


def test_browser_formatters_and_insight_units_are_currency_aware():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser formatter regression")
    root      = REPOSITORY_ROOT
    formatter = (root / "plugins/pendo/web/static/js/utils/format.js").as_uri()
    component = (root / "plugins/pendo/web/static/js/components/ledger_insights.js").as_uri()
    script    = f"""
import {{ formatAmount, formatMoneyCompact }} from {json.dumps(formatter)};
import {{ renderLedgerInsightsPanel }} from {json.dumps(component)};
const html = renderLedgerInsightsPanel({{currency:'USD',summary:{{focus_total:200,focus_count:1}},expense_timeline:[{{key:'2026-03-01',total:200,count:1}}],expense_categories:[{{category:'food',total:200,count:1}}]}});
if (formatAmount(200,'USD') !== 'USD 200.00') throw Error('amount unit');
if (formatMoneyCompact(200,'USD') !== 'USD 200') throw Error('compact unit');
if (!html.includes('USD 200') || html.includes('¥')) throw Error('chart unit');
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        check          = True,
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        errors         = "replace",
        timeout        = 15,
    )
