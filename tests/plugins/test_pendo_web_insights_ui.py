"""Pendo Web 账本洞察和页面契约。"""

from __future__ import annotations

from tests.helpers.pendo_web_items_test_support import (
    ROOT,
    Database,
    build_ledger_insights,
    datetime,
    ledger_insights_module,
    pytest,
    re,
    shutil,
    uuid,
)


def test_build_ledger_insights_uses_filtered_ledger_category_and_builds_svg_data():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_insights_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-insights"

    try:
        db.insert_item(
            {
                "id": "e1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "早餐",
                "amount": 12,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-20",
                "created_at": "2026-03-20T08:00:00",
            }
        )
        db.insert_item(
            {
                "id": "e2",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午餐",
                "amount": 24,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-20",
                "created_at": "2026-03-20T12:00:00",
            }
        )
        db.insert_item(
            {
                "id": "e3",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "地铁",
                "amount": 6,
                "transaction_type": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-03-21",
                "created_at": "2026-03-21T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "i1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "工资",
                "amount": 5000,
                "transaction_type": "income",
                "ledger_category": "工资",
                "ledger_date": "2026-03-21",
                "created_at": "2026-03-21T10:00:00",
            }
        )

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            category="餐饮",
            start_date="2026-03-20",
            end_date="2026-03-21",
        )

        assert result["summary"]["expense_total"] == 36
        assert result["summary"]["income_total"] == 0
        assert result["summary"]["focus_transaction_type"] == "expense"
        assert result["summary"]["focus_count"] == 2
        assert len(result["expense_categories"]) == 1
        assert result["expense_categories"][0]["category"] == "餐饮"
        assert result["expense_categories"][0]["share"] == 1
        assert [point["total"] for point in result["expense_timeline"]] == [36, 0]
        assert len(result["expense_candles"]) == 1
        assert result["expense_candles"][0]["open"] == 12
        assert result["expense_candles"][0]["close"] == 24
        assert result["expense_candles"][0]["high"] == 24
        assert result["expense_candles"][0]["low"] == 12
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_switches_focus_with_income_filter():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_income_insights_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-income-insights"

    try:
        for item in [
            {
                "id": "income-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "稿费",
                "amount": 500,
                "transaction_type": "income",
                "ledger_category": "副业",
                "ledger_date": "2026-03-02",
                "created_at": "2026-03-02T09:00:00",
            },
            {
                "id": "income-2",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "奖金",
                "amount": 800,
                "transaction_type": "income",
                "ledger_category": "奖金",
                "ledger_date": "2026-03-18",
                "created_at": "2026-03-18T09:00:00",
            },
            {
                "id": "expense-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 30,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-10",
                "created_at": "2026-03-10T12:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            transaction_type="income",
            start_date="2026-03-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["focus_transaction_type"] == "income"
        assert result["summary"]["focus_total"] == 1300
        assert result["summary"]["focus_count"] == 2
        assert [item["category"] for item in result["expense_categories"]] == ["奖金", "副业"]
        timeline = {
            point["key"]: point["total"] for point in result["expense_timeline"] if point["total"]
        }
        assert timeline == {
            "2026-03-02": 500,
            "2026-03-18": 800,
        }
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_year_mode_compares_against_last_year_to_date():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_year_compare_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-year-compare"

    try:
        db.insert_item(
            {
                "id": "cy1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "今年一月",
                "amount": 100,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-01-10",
                "created_at": "2026-01-10T10:00:00",
            }
        )
        db.insert_item(
            {
                "id": "cy2",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "今年三月",
                "amount": 50,
                "transaction_type": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-03-10",
                "created_at": "2026-03-10T10:00:00",
            }
        )
        db.insert_item(
            {
                "id": "py1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "去年同期",
                "amount": 100,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2025-02-10",
                "created_at": "2025-02-10T10:00:00",
            }
        )
        db.insert_item(
            {
                "id": "pp1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "上一周期高支出",
                "amount": 500,
                "transaction_type": "expense",
                "ledger_category": "服务",
                "ledger_date": "2025-11-10",
                "created_at": "2025-11-10T10:00:00",
            }
        )

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-03-25",
            compare_mode="previous_year_to_date",
        )

        assert result["summary"]["expense_total"] == 150
        assert result["summary"]["delta_label"] == "较去年同期"
        assert result["summary"]["delta_vs_previous"] == 0.5
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_month_bucket_orders_candles_by_ledger_date_not_created_at():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_month_candles_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-month-candles"

    try:
        for item in [
            {
                "id": "m-backfill",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月初补录",
                "amount": 10,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-01",
                "created_at": "2026-04-01T09:00:00",
            },
            {
                "id": "m-mid",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月中消费",
                "amount": 25,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-05",
                "created_at": "2026-03-05T09:00:00",
            },
            {
                "id": "m-end",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月底消费",
                "amount": 40,
                "transaction_type": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-03-28",
                "created_at": "2026-03-28T09:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["bucket_mode"] == "month"
        assert result["expense_timeline"][-1]["key"] == "2026-03"
        assert result["expense_timeline"][-1]["total"] == 75
        assert result["expense_candles"][-1]["label"] == "2026-03"
        assert result["expense_candles"][-1]["open"] == 10
        assert result["expense_candles"][-1]["close"] == 40
        assert result["expense_candles"][-1]["high"] == 40
        assert result["expense_candles"][-1]["low"] == 10
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_clips_current_period_visuals_to_user_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_current_period_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-current-period"

    def user_now(user_id: str, database: Database) -> datetime:
        assert user_id == owner_id
        assert database is db
        return datetime.fromisoformat("2026-04-08T10:00:00-12:00")

    monkeypatch.setattr(
        ledger_insights_module,
        "now_in_timezone",
        user_now,
    )

    try:
        for item in [
            {
                "id": "apr-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月初消费",
                "amount": 48,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-04-01",
                "created_at": "2026-04-01T09:00:00",
            },
            {
                "id": "apr-8",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "今天消费",
                "amount": 28,
                "transaction_type": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-04-08",
                "created_at": "2026-04-08T09:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-04-01",
            end_date="2026-04-30",
        )

        assert result["summary"]["focus_total"] == 76
        assert result["summary"]["peak_bucket_label"] == "4/1"
        assert result["expense_timeline"][0]["key"] == "2026-04-01"
        assert result["expense_timeline"][-1]["key"] == "2026-04-08"
        assert len(result["expense_timeline"]) == 8
        assert result["expense_candles"][-1]["key"] == "2026-04-08"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_keeps_empty_range_peak_blank(db: Database) -> None:
    result = build_ledger_insights(
        db=db,
        owner_id="u-empty-insights",
        start_date="2026-01-01",
        end_date="2026-01-02",
    )

    assert [point["count"] for point in result["expense_timeline"]] == [0, 0]
    assert result["summary"]["peak_bucket_label"] == ""
    assert result["summary"]["peak_bucket_total"] == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"transaction_type": "refund"}, "transaction_type must be"),
        ({"compare_mode": "quarter"}, "compare_mode must be"),
        (
            {"start_date": "2026-03-02", "end_date": "2026-03-01"},
            "start_date must not be after end_date",
        ),
        ({"amount_min": -1}, "amount_min must not be negative"),
        ({"amount_min": 20, "amount_max": 10}, "amount_min must not be greater"),
    ],
)
def test_build_ledger_insights_rejects_invalid_direct_arguments(
    db: Database,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_ledger_insights(db=db, owner_id="u-invalid-insights", **kwargs)


def test_pendo_web_pages_use_unified_xl_mobile_phone_breakpoints():
    roots = [
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages",
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components",
    ]
    legacy_tokens = [
        "BREAKPOINTS.WIDE",
        "BREAKPOINTS.NARROW",
        "BREAKPOINTS.COMPACT",
        "BREAKPOINTS.FORM",
        "BREAKPOINTS.SEARCH",
        "BREAKPOINTS.DASHBOARD",
        "BREAKPOINTS.DESKTOP",
        "BREAKPOINTS.STATS_SMALL",
    ]

    for root in roots:
        for path in root.rglob("*.js"):
            src = path.read_text(encoding="utf-8")
            for token in legacy_tokens:
                assert token not in src, f"{path} still uses legacy breakpoint {token}"


def test_app_and_global_styles_define_one_back_to_top_component() -> None:
    """入口只负责按钮行为，静态外观归入全局样式且保留主题与焦点反馈。"""

    app_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    css_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "css" / "app.css").read_text(
        encoding="utf-8"
    )

    assert "const BACK_TO_TOP_THEME = {" in app_src
    assert "btn.type = 'button';" in app_src
    assert "onRouteChange(applyTheme);" in app_src
    assert "getCurrentPage" not in app_src
    assert "document.createElement('style')" not in app_src
    assert re.search(
        r"window\.matchMedia\(\s*['\"]\(prefers-reduced-motion: reduce\)['\"]\s*,?\s*\)",
        app_src,
    )
    assert "width: 38px;" in css_src
    assert "height: 38px;" in css_src
    assert "--btt-accent: var(--color-dashboard);" in css_src
    assert "background: color-mix(in srgb, var(--btt-accent) 68%, transparent);" in css_src
    assert "-webkit-tap-highlight-color: transparent;" in css_src
    assert "#back-to-top:focus-visible {" in css_src
    assert "color-mix(in srgb, var(--btt-accent) 16%, transparent);" in css_src


def test_app_source_extracts_one_time_login_code_from_pasted_message() -> None:
    """登录入口应从完整链接或聊天文本中提取一次性登录码。"""

    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    html = (ROOT / "plugins" / "pendo" / "web" / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "function extractLoginCode(rawValue)" in src
    assert "url.searchParams.get('code')" in src
    assert "const code = extractLoginCode(input.value);" in src
    assert "if (code !== input.value.trim()) {" in src
    assert "一次性登录链接" in html
