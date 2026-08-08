"""Pendo Web 看板的数据边界、原生图表和异步生命周期回归。"""

from __future__ import annotations

import re
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
DASHBOARD_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js"
)
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"

DASHBOARD_SETUP: Final = r"""
    globalThis.__api = {
        get: async () => ({ data: {} }),
        put: async () => ({ data: {} }),
    };
    globalThis.__toastCalls = [];
    globalThis.__showToast = (...args) => __toastCalls.push(args);
    globalThis.__styleCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__subscribeDataChanges = (_type, callback) => {
        __dataChangeCallback = callback;
        return () => { __unsubscribeCount += 1; };
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
    };
"""


def _dashboard_source_for_test() -> str:
    """替换浏览器相邻依赖，并复用真实共享格式化实现。"""

    source = DASHBOARD_CLIENT.read_text(encoding="utf-8")
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{
    errorMessage,
    finiteNumber,
    formatAmount,
    formatMoneyCompact,
    isRecord,
    isoDate,
    nonNegativeInteger,
    pad2,
    parseDate,
    records,
}} = (() => {{
{format_source}
    return {{
        errorMessage,
        finiteNumber,
        formatAmount,
        formatMoneyCompact,
        isRecord,
        isoDate,
        nonNegativeInteger,
        pad2,
        parseDate,
        records,
    }};
}})();
"""
    import_replacements = (
        ("../api.js", "const api = globalThis.__api;"),
        (
            "../components/toast.js",
            "const showToast = (...args) => globalThis.__showToast(...args);",
        ),
        ("../utils/format.js", format_runtime),
        (
            "../utils/ui.js",
            """const BREAKPOINTS = { XL: '1200px', MOBILE: '720px' };
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (_breakpoint, cssText) => cssText;
const pageShellCss = () => '';
const subscribeDataChanges = (...args) => globalThis.__subscribeDataChanges(...args);""",
        ),
    )
    for module_path, replacement in import_replacements:
        pattern = r"import\s*\{[^;]*\}\s*from\s*['\"]" + re.escape(module_path) + r"['\"]\s*;"
        source, count = re.subn(
            pattern,
            lambda _match, value=replacement: value,
            source,
            count=1,
        )
        assert count == 1, f"missing named import from {module_path}"

    return source + (
        "\nexport { buildDashboardMarkup, buildSpendingAxisTicks, "
        "normalizeDashboardData, renderFinancePanel, renderSpendingChart };\n"
    )


def _run_dashboard_client(script: str) -> None:
    """在 Node 中执行看板真实 ESM 计算、渲染与生命周期。"""

    assert_node_esm_contract(
        _dashboard_source_for_test(),
        script,
        cwd=ROOT,
        setup=DASHBOARD_SETUP,
    )


def test_dashboard_chart_normalizes_values_dates_and_native_markup() -> None:
    """损坏金额或日期不能污染坐标，图表应直接生成安全的原生标记。"""

    _run_dashboard_client(
        r"""
        assert.deepEqual(
            client.buildSpendingAxisTicks([
                0, 10, -5, Number.NaN, Number.POSITIVE_INFINITY, Symbol('bad'),
            ]),
            [0, 3, 7, 10],
        );
        assert.deepEqual(client.buildSpendingAxisTicks([]), [0, 1]);
        assert.deepEqual(client.buildSpendingAxisTicks('bad'), [0, 1]);

        const chart = client.renderSpendingChart([
            { date: '2026-03-01', amount: 0 },
            { date: '2026-03-02', amount: 25.5 },
            { date: '2026-02-30', amount: Number.NaN },
            { date: '"><script>alert(1)</script>', amount: 10 },
        ]);
        assert.match(chart, /role="img"/);
        assert.match(chart, /最高单日 ¥25\.50/);
        assert.match(chart, /class="dashboard-spending-chart"/);
        assert.match(chart, /<polygon class="dashboard-chart-area"/);
        assert.match(chart, /<polyline class="dashboard-chart-line"/);
        assert.equal((chart.match(/class="dashboard-chart-point"/g) || []).length, 4);
        assert.ok(chart.includes('03/01'));
        assert.ok(chart.includes('未知日期'));
        assert.ok(!chart.includes('<script>'));
        assert.ok(!chart.includes('NaN'));
        assert.ok(!chart.includes('Infinity'));
        assert.ok(!chart.includes('<canvas'));
        assert.ok(!chart.includes('https://'));
        assert.equal(client.renderSpendingChart([]), '');
        assert.equal(client.renderSpendingChart([null, 'bad']), '');
        """
    )


def test_dashboard_markup_normalizes_response_and_uses_accessible_navigation() -> None:
    """接口异常字段应在单一边界收敛，趋势入口应使用可键盘操作的原生链接。"""

    _run_dashboard_client(
        r"""
        const markup = client.buildDashboardMarkup({
            summary: {
                events_month: '<script>count</script>',
                tasks_pending: -3,
                tasks_done_recent: 2.9,
                ledger_month_expense: Number.POSITIVE_INFINITY,
                diary_month: 4,
            },
            events_agenda: [{
                start_time: '2099-03-05T09:00:00',
                end_time: '2099-03-05T10:00:00',
                title: '<img src=x onerror=alert(1)>',
                location: '<script>place</script>',
            }],
            tasks: {
                active: [
                    { id: 'task/a b', title: '<svg onload=alert(1)>', priority: 2 },
                    { id: '', title: '缺少编号' },
                ],
                completed: [{ title: '完成项', completed_at: 'not-a-date' }],
            },
            spending_trend: [{ date: 'bad-date', amount: Number.NaN }],
            month_summary: { income: Symbol('bad'), expense: -9, balance: -8 },
            recent_ledger: [{
                title: '<script>ledger</script>',
                ledger_category: '<b>分类</b>',
                ledger_date: '2026-02-30',
                amount: -12,
                transaction_type: 'unknown',
            }],
        });

        assert.ok(markup.includes('<a class="dashboard-chart-card" href="#/stats">'));
        assert.ok(!markup.includes('dashboard-chart-card" id='));
        assert.ok(markup.includes('data-task-id="task/a b"'));
        assert.match(markup, /<input type="checkbox" disabled aria-disabled="true"/);
        assert.ok(markup.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(markup.includes('&lt;svg onload=alert(1)&gt;'));
        assert.ok(markup.includes('&lt;script&gt;ledger&lt;/script&gt;'));
        assert.ok(markup.includes('未知日期'));
        assert.ok(markup.includes('dashboard-finance-value negative">-¥8.00'));
        assert.ok(!markup.includes('<script>'));
        assert.ok(!markup.includes('NaN'));
        assert.ok(!markup.includes('Infinity'));
        assert.ok(!markup.includes('style="color:'));
        """
    )


def test_dashboard_ignores_stale_or_destroyed_loads() -> None:
    """后发请求应胜出，销毁后的慢响应不得覆盖其他页面。"""

    _run_dashboard_client(
        r"""
        const requests = [];
        __api.get = () => new Promise((resolve, reject) => requests.push({ resolve, reject }));
        class FakeContainer {
            constructor() { this.innerHTML = ''; }
            querySelector() { return null; }
        }
        const container = new FakeContainer();

        assert.throws(() => client.render(null), /看板需要有效的 DOM 挂载容器/);
        client.render(container);
        assert.equal(requests.length, 1);
        assert.match(container.innerHTML, /加载中/);

        __dataChangeCallback();
        assert.equal(requests.length, 2);
        requests[1].resolve({ data: { summary: { diary_month: 2 } } });
        await __flushPromises();
        const newestMarkup = container.innerHTML;
        assert.match(newestMarkup, /近 30 天共写了 2 篇/);

        requests[0].resolve({ data: { summary: { diary_month: 99 } } });
        await __flushPromises();
        assert.equal(container.innerHTML, newestMarkup);

        client.destroy();
        assert.equal(__unsubscribeCount, 1);
        client.render(container);
        assert.equal(requests.length, 3);
        client.destroy();
        container.innerHTML = '<main>其他页面</main>';
        requests[2].resolve({ data: { summary: { diary_month: 88 } } });
        await __flushPromises();
        assert.equal(container.innerHTML, '<main>其他页面</main>');
        assert.equal(__unsubscribeCount, 2);
        """
    )


def test_dashboard_task_completion_encodes_id_and_blocks_duplicate_requests() -> None:
    """任务更新应编码路径，提交期间忽略重复事件，并在失败后恢复控件。"""

    _run_dashboard_client(
        r"""
        __api.get = async () => ({
            data: { tasks: { active: [{ id: 'task/a b', title: '待完成' }] } },
        });
        const putCalls = [];
        __api.put = async (...args) => {
            putCalls.push(args);
            throw new Error('update failed');
        };
        const taskList = {
            listener: null,
            addEventListener(type, callback) {
                assert.equal(type, 'change');
                this.listener = callback;
            },
        };
        const container = {
            innerHTML: '',
            querySelector: (selector) => selector === '#dashboard-active-tasks' ? taskList : null,
        };
        client.render(container);
        await __flushPromises();
        assert.equal(typeof taskList.listener, 'function');

        const checkbox = {
            checked: true,
            disabled: false,
            dataset: { taskId: 'task/a b' },
            matches: () => true,
        };
        const first = taskList.listener({ target: checkbox });
        const duplicate = taskList.listener({ target: checkbox });
        await Promise.all([first, duplicate]);

        assert.deepEqual(putCalls, [['/items/task%2Fa%20b', { status: 'done' }]]);
        assert.deepEqual(__toastCalls, [['标记失败：update failed', 'error']]);
        assert.equal(checkbox.checked, false);
        assert.equal(checkbox.disabled, false);
        client.destroy();
        """
    )
