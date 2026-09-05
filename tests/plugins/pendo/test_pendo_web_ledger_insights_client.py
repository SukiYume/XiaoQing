"""Pendo Web 账目洞察图表的渲染、边界和安全回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final                   = REPOSITORY_ROOT
LEDGER_INSIGHTS_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "ledger_insights.js"
)

LEDGER_INSIGHTS_SETUP: Final = r"""
    globalThis.__format = {
        arrayValue: (value) => Array.isArray(value) ? value : [],
        formatAmount: (value) => `¥${Number(value).toFixed(2)}`,
        formatMoneyCompact: (value) => `¥${Number(value).toFixed(1)}`,
    };
    globalThis.__ui = {
        escapeHtml: (value) => String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;'),
    };
"""


def _ledger_insights_source_for_test() -> str:
    """只替换相邻格式化依赖，保留洞察组件真实实现。"""

    source = LEDGER_INSIGHTS_CLIENT.read_text(encoding="utf-8")
    format_import = (
        "import { arrayValue as asArray, formatAmount, formatMoneyCompact } "
        "from '../utils/format.js';"
    )
    ui_import = "import { escapeHtml } from '../utils/ui.js';"
    assert format_import in source
    assert ui_import in source
    return source.replace(
        format_import,
        "const { arrayValue: asArray, formatAmount, formatMoneyCompact } = globalThis.__format;",
    ).replace(ui_import, "const { escapeHtml } = globalThis.__ui;")


def _run_ledger_insights_client(script: str) -> None:
    """在 Node 中执行无构建图表模块。"""

    assert_node_esm_contract(
        _ledger_insights_source_for_test(),
        script,
        cwd   = ROOT,
        setup = LEDGER_INSIGHTS_SETUP,
    )


def test_ledger_insights_renders_all_cards_and_keeps_first_and_last_axis_labels() -> None:
    """十一段时间轴应稳定保留首尾标签，并完整渲染四类洞察。"""

    _run_ledger_insights_client(
        r"""
        const timeline = Array.from({ length: 11 }, (_, index) => ({
            key: `2026-07-${String(index + 1).padStart(2, '0')}`,
            label: `7/${index + 1}`,
            total: index + 1,
            count: index,
        }));
        const categories = Array.from({ length: 6 }, (_, index) => ({
            category: `分类${index + 1}`,
            total: 60 - index * 5,
            count: index + 1,
            share: 0.1,
        }));
        const html = client.renderLedgerInsightsPanel({
            summary: {
                focus_transaction_type: 'expense',
                focus_total: 285,
                average_focus_amount: 9.5,
                bucket_mode: 'day',
                peak_bucket_label: '7/11',
                peak_bucket_total: 11,
                delta_label: '较上一周期',
                delta_vs_previous: -0.125,
            },
            expense_timeline: timeline,
            expense_categories: categories,
            expense_hotspots: categories,
            expense_candles: [
                { key: '2026-07-01', label: '7/1', open: 2, close: 4, high: 5, low: 1 },
                { key: '2026-07-11', label: '7/11', open: 7, close: 6, high: 9, low: 5 },
            ],
        });

        for (const className of [
            'ledger-insight-card-pulse',
            'ledger-insight-card-ring',
            'ledger-insight-card-hotspot',
            'ledger-insight-card-kline',
            'ledger-insight-svg-trend',
            'ledger-insight-svg-ring',
            'ledger-insight-svg-kline',
        ]) assert.ok(html.includes(className));
        assert.ok(html.includes('>7/1</text>'));
        assert.ok(html.includes('>7/11</text>'));
        assert.ok(html.includes('其他'));
        assert.ok(html.includes('class="is-down">-13%'));
        assert.ok(!html.includes('NaN'));
        assert.ok(!html.includes('Infinity'));
        """
    )


def test_ledger_insights_escapes_server_text_and_normalizes_broken_numbers() -> None:
    """异常 API 数据不得进入 HTML、坐标、百分比或内联宽度。"""

    _run_ledger_insights_client(
        r"""
        const attack = '</title><script>bad()</script>';
        const html = client.renderLedgerInsightsPanel({
            summary: {
                focus_transaction_type: '__proto__',
                focus_total: 10,
                average_focus_amount: Infinity,
                peak_bucket_label: attack,
                peak_bucket_total: NaN,
                delta_label: attack,
                delta_vs_previous: Infinity,
            },
            expense_timeline: [
                { key: '2026-07-01', label: attack, total: 10, count: attack },
                { key: '2026-07-02', label: '损坏', total: Infinity, count: -3 },
            ],
            expense_categories: [
                { category: attack, total: 50, count: attack },
                { category: '负数', total: -20, count: 1 },
            ],
            expense_hotspots: [{ category: attack, total: 50, count: attack }],
            expense_candles: [
                {
                    key: 'outside', label: attack, open: Infinity, close: 3,
                    high: NaN, low: -4,
                },
            ],
        });

        assert.ok(html.includes('&lt;/title&gt;&lt;script&gt;bad()&lt;/script&gt;'));
        assert.ok(!html.includes('<script>'));
        assert.ok(!html.includes('NaN'));
        assert.ok(!html.includes('Infinity'));
        assert.ok(html.includes('style="width:100.00%;"'));
        assert.ok(html.includes('无对比'));
        assert.ok(html.includes('支出脉搏'));

        const empty = client.renderLedgerInsightsPanel(null);
        assert.equal((empty.match(/ledger-insight-empty/g) || []).length, 4);
        const malformed = client.renderLedgerInsightsPanel({
            summary: [], expense_timeline: {}, expense_categories: 'bad',
            expense_hotspots: null, expense_candles: false,
        });
        assert.equal((malformed.match(/ledger-insight-empty/g) || []).length, 4);
        """
    )


def test_ledger_insights_handles_focus_copy_first_spend_and_complete_ring_geometry() -> None:
    """收入/转账文案、首次支出语义和单分类完整圆环应保持原契约。"""

    _run_ledger_insights_client(
        r"""
        const base = {
            expense_timeline: [{ key: '2026-07', label: '2026-07', total: 20, count: 1 }],
            expense_categories: [{ category: '工资', total: 20, count: 1 }],
            expense_candles: [
                { key: '2026-07', label: '2026-07', open: 20, close: 20, high: 20, low: 20 },
            ],
        };
        const income = client.renderLedgerInsightsPanel({
            ...base,
            summary: {
                focus_transaction_type: 'income', focus_total: 20,
                delta_vs_previous: 1, delta_label: '较上一周期', bucket_mode: 'month',
            },
        });
        assert.ok(income.includes('收入脉搏'));
        assert.ok(income.includes('入账节奏'));
        assert.ok(income.includes('首次有收入'));
        assert.ok(income.includes('>按月</span>'));
        assert.ok(income.includes('A 84 84 0 1 1'));

        const transfer = client.renderLedgerInsightsPanel({
            ...base,
            summary: { focus_transaction_type: 'transfer', focus_total: 20 },
        });
        assert.ok(transfer.includes('转账脉搏'));
        assert.ok(transfer.includes('流转节奏'));
        """
    )
