"""Pendo Web 账本页的数据边界、表单操作与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
LEDGER_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "ledger.js"
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"

LEDGER_SETUP: Final = r"""
    globalThis.__api = {
        get: async () => ({ data: {} }),
        post: async () => ({ data: {} }),
        put: async () => ({ data: {} }),
        delete: async () => ({ data: {} }),
    };
    globalThis.__toastCalls = [];
    globalThis.__modalCalls = [];
    globalThis.__modalContent = null;
    globalThis.__closeModalCount = 0;
    globalThis.__confirmResult = true;
    globalThis.__formData = {};
    globalThis.__builtFields = [];
    globalThis.__styleCalls = [];
    globalThis.__paginationCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__dispatchedEvents = [];
    globalThis.__subscribeDataChanges = (_type, callback) => {
        __dataChangeCallback = callback;
        return () => { __unsubscribeCount += 1; };
    };
    globalThis.window = {
        dispatchEvent(event) { __dispatchedEvents.push(event); return true; },
    };
    globalThis.CustomEvent = class {
        constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    };
"""


def _ledger_source_for_test() -> str:
    """替换浏览器相邻依赖，并嵌入真实共享金额与日期校验实现。"""

    source = LEDGER_CLIENT.read_text(encoding="utf-8")
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{ finiteNumber, formatAmount, isValidDateInput, nonNegativeInteger }} = (() => {{
{format_source}
    return {{ finiteNumber, formatAmount, isValidDateInput, nonNegativeInteger }};
}})();
"""
    replacements = (
        ("import { api } from '../api.js';", "const api = globalThis.__api;"),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            "import { showModal, closeModal, showConfirmModal, safeHtml } "
            "from '../components/modal.js';",
            """const showModal = (...args) => {
    globalThis.__modalCalls.push(args);
    return globalThis.__modalContent;
};
const closeModal = () => { globalThis.__closeModalCount += 1; };
const showConfirmModal = async () => globalThis.__confirmResult;
const safeHtml = (value) => value;""",
        ),
        (
            "import { buildFormHTML, getFormData, initFormInteractions } "
            "from '../components/form.js';",
            """const buildFormHTML = (fields) => {
    globalThis.__builtFields = fields;
    return '<div class="stub-form-fields"></div>';
};
const getFormData = () => ({ ...globalThis.__formData });
const initFormInteractions = () => {};""",
        ),
        (
            "import { renderPagination } from '../components/pagination.js';",
            "const renderPagination = (...args) => globalThis.__paginationCalls.push(args);",
        ),
        (
            "import { renderLedgerInsightsPanel } from '../components/ledger_insights.js';",
            "const renderLedgerInsightsPanel = () => '<section class=\"stub-insights\"></section>';",
        ),
        (
            "import { renderCustomSelect, initCustomSelects } "
            "from '../components/custom_select.js';",
            """const renderCustomSelect = ({ id, options = [], selected = '' }) => `
    <div class="stub-select" id="${escapeHtml(id)}" data-value="${escapeHtml(selected)}">
        ${options.map((option) => `<span data-option="${escapeHtml(option.value)}">${escapeHtml(option.label)}</span>`).join('')}
    </div>`;
const initCustomSelects = () => {};""",
        ),
        (
            """import {
    finiteNumber,
    formatAmount,
    isValidDateInput,
    nonNegativeInteger,
} from '../utils/format.js';""",
            format_runtime,
        ),
        (
            "import { derivePresetRange, todayRangeKey } from '../utils/date_ranges.js';",
            """const todayRangeKey = () => '2026-03-15';
const derivePresetRange = (preset, options = {}) => {
    if (preset === 'today') return { start: options.today, end: options.today };
    if (preset === 'custom') return {
        start: options.customStart || '', end: options.customEnd || '',
    };
    if (preset === 'all') return { start: '1970-01-01', end: options.today };
    if (preset === 'week') return { start: '2026-03-09', end: '2026-03-15' };
    return { start: '2026-03-01', end: '2026-03-31' };
};""",
        ),
        (
            """import {
    bindEnterAction,
    bindFormSubmit,
    BREAKPOINTS,
    escapeHtml,
    injectStyles,
    mediaMax,
    pageShellCss,
    subscribeDataChanges,
} from '../utils/ui.js';""",
            """const bindEnterAction = (element, action) => {
    if (!element || typeof action !== 'function') return;
    element.onkeydown = async (event) => {
        if (event.key === 'Enter' && !event.isComposing) {
            event.preventDefault();
            await action();
        }
    };
};
const bindFormSubmit = (form, submitButton) => {
    if (!form || !submitButton) return;
    form.onsubmit = (event) => {
        event.preventDefault();
        submitButton.click();
    };
};
const BREAKPOINTS = {
    XL: '1200px', MOBILE: '720px', PHONE: '560px',
};
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
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    currentFilterParams,
    dateRangeForFilter,
    fetchAggregate,
    fetchAccounts,
    fetchCategories,
    fetchInsights,
    fetchItems,
    groupByDate,
    handleDelete as __handleDelete,
    handleQuickAdd as __handleQuickAdd,
    ensureStyles as __ensureStyles,
    loadAndRender as __loadAndRender,
    normalizeLedgerItem,
    normalizeSummary,
    openDetailModal as __openDetailModal,
    openEditModal as __openEditModal,
    parseAmountInput,
    renderFilterBar,
    renderItemRow,
    renderList,
    renderSummaryCards,
};
export function __setLedgerTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('items' in state) _items = state.items;
    if ('total' in state) _total = state.total;
    if ('page' in state) _page = state.page;
    if ('dateFilter' in state) _dateFilter = state.dateFilter;
    if ('transactionTypeFilter' in state) {
        _transactionTypeFilter = state.transactionTypeFilter;
    }
    if ('categoryFilter' in state) _categoryFilter = state.categoryFilter;
    if ('accountFilter' in state) _accountFilter = state.accountFilter;
    if ('amountMin' in state) _amountMin = state.amountMin;
    if ('amountMax' in state) _amountMax = state.amountMax;
    if ('sortMode' in state) _sortMode = state.sortMode;
    if ('customDateStart' in state) _customDateStart = state.customDateStart;
    if ('customDateEnd' in state) _customDateEnd = state.customDateEnd;
    if ('summaryData' in state) _summaryData = state.summaryData;
    if ('insightsData' in state) _insightsData = state.insightsData;
    if ('categories' in state) _allCategories = state.categories;
    if ('accounts' in state) _allAccounts = state.accounts;
    _quickAddSaving = false;
}
"""
    )


def _run_ledger_client(script: str) -> None:
    """在 Node 中执行账本页真实 ESM 数据、渲染、表单和生命周期。"""

    assert_node_esm_contract(
        _ledger_source_for_test(),
        script,
        cwd=ROOT,
        setup=LEDGER_SETUP,
    )


def test_ledger_amount_item_and_summary_boundaries() -> None:
    """金额必须完整匹配有限数值，损坏账目与汇总不得污染页面。"""

    _run_ledger_client(
        r"""
        assert.equal(client.parseAmountInput('12.50'), 12.5);
        assert.equal(client.parseAmountInput('.5'), 0.5);
        assert.equal(client.parseAmountInput('12yuan'), null);
        assert.equal(client.parseAmountInput('-1'), null);
        assert.equal(client.parseAmountInput('Infinity'), null);
        assert.equal(client.parseAmountInput('0', { allowZero: false }), null);
        assert.equal(client.parseAmountInput(Symbol('bad')), null);

        const item = client.normalizeLedgerItem({
            id: 'ledger-1',
            transaction_type: '"><script>type</script>',
            amount: Number.NEGATIVE_INFINITY,
            title: '<img src=x onerror=alert(1)>',
            ledger_category: '<script>category</script>',
            ledger_date: '2026-02-30',
        });
        assert.equal(item.transaction_type, 'expense');
        assert.equal(item.amount, 0);
        assert.equal(item.ledger_date, '');
        assert.equal(item.title, '<img src=x onerror=alert(1)>');

        assert.deepEqual(client.normalizeSummary({
            income: Number.POSITIVE_INFINITY,
            expense: -5,
            transfer: 12.9,
            balance: Symbol('bad'),
            count: 3.9,
        }), { income: 0, expense: 0, transfer: 12.9, balance: 0, count: 3 });
        assert.equal(client.normalizeLedgerItem(null), null);
        """
    )


def test_ledger_filters_share_validated_range_and_amount_params() -> None:
    """列表、汇总和洞察应复用同一筛选边界，包括“全部”截止今天。"""

    _run_ledger_client(
        r"""
        client.__setLedgerTestState({
            dateFilter: 'all',
            transactionTypeFilter: 'expense',
            categoryFilter: '餐饮',
            accountFilter: '微信',
            amountMin: '10.50',
            amountMax: '20',
            sortMode: 'amount',
        });
        assert.deepEqual(client.dateRangeForFilter('all'), {
            start_date: '1970-01-01', end_date: '2026-03-15',
        });
        assert.deepEqual(client.currentFilterParams(), {
            start_date: '1970-01-01',
            end_date: '2026-03-15',
            transaction_type: 'expense',
            category: '餐饮',
            account_name: '微信',
            amount_min: 10.5,
            amount_max: 20,
        });

        const calls = [];
        __api.get = async (path, params) => {
            calls.push({ path, params });
            if (path === '/items') {
                return {
                    data: {
                        items: [null, {
                            id: 'l1', title: '午饭', amount: 18,
                            ledger_date: '2026-03-02', transaction_type: 'expense',
                        }],
                        total: '3',
                    },
                };
            }
            if (path === '/items/aggregate') {
                return { data: { income: '5', expense: 18, balance: -13, count: 1 } };
            }
            return { data: { expense_total: 18 } };
        };
        const [items, aggregate, insights] = await Promise.all([
            client.fetchItems(2), client.fetchAggregate(), client.fetchInsights(),
        ]);
        assert.equal(items.items.length, 1);
        assert.equal(items.total, 3);
        assert.equal(aggregate.income, 5);
        assert.equal(insights.expense_total, 18);
        assert.equal(calls.length, 3);
        for (const call of calls) {
            assert.equal(call.params.start_date, '1970-01-01');
            assert.equal(call.params.end_date, '2026-03-15');
            assert.equal(call.params.amount_min, 10.5);
            assert.equal(call.params.amount_max, 20);
        }
        assert.equal(calls[0].params.sort, 'amount');
        assert.equal(calls[0].params.page, 2);
        assert.equal(calls[2].params.compare_mode, 'none');
        """
    )


def test_ledger_renderers_escape_values_and_use_native_controls() -> None:
    """筛选、列表、汇总和详情应安全转义并提供原生键盘入口。"""

    _run_ledger_client(
        r"""
        client.__setLedgerTestState({
            dateFilter: 'custom',
            customDateStart: '"><script>start</script>',
            customDateEnd: '2026-03-15',
            amountMin: '"><img src=x>',
            amountMax: '20',
            categories: ['<script>category</script>'],
            accounts: ['<img src=x onerror=alert(1)>'],
            summaryData: client.normalizeSummary({
                income: Number.POSITIVE_INFINITY,
                expense: 12,
                transfer: 4,
                balance: -8,
            }),
        });
        const filters = client.renderFilterBar();
        assert.ok(filters.includes('&quot;&gt;&lt;script&gt;start&lt;/script&gt;'));
        assert.ok(filters.includes('&lt;script&gt;category&lt;/script&gt;'));
        assert.ok(filters.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(!filters.includes('id="filter-custom-range" hidden'));
        assert.ok(!filters.includes('<script>'));

        const item = client.normalizeLedgerItem({
            id: 'ledger/&quot; bad',
            transaction_type: '"><script>type</script>',
            amount: Number.NaN,
            title: '<img src=x onerror=alert(1)>',
            ledger_category: '<script>category</script>',
            ledger_date: '2026-03-02',
            account_name: '<b>账户</b>',
            merchant: '<svg onload=alert(1)>',
            remark: '<script>remark</script>',
        });
        const row = client.renderItemRow(item);
        assert.match(row, /<button type="button" class="ledger-row-main"/);
        assert.ok(row.includes('ledger-amount-expense'));
        assert.ok(row.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(row.includes('&lt;script&gt;category&lt;/script&gt;'));
        assert.ok(!row.includes('style="'));
        assert.ok(!row.includes('<script>'));
        assert.ok(!row.includes('NaN'));

        const summary = client.renderSummaryCards();
        assert.ok(summary.includes('ledger-amount-expense'));
        assert.ok(!summary.includes('Infinity'));
        assert.ok(!summary.includes('style="'));

        const buttons = new Map([
            ['#detail-close', { onclick: null }],
            ['#detail-edit', { onclick: null, disabled: false }],
        ]);
        __modalContent = { querySelector: (selector) => buttons.get(selector) || null };
        client.__openDetailModal(item);
        const [, body, options] = __modalCalls.at(-1);
        assert.ok(body.includes('&lt;script&gt;remark&lt;/script&gt;'));
        assert.ok(!body.includes('style="'));
        assert.ok(!body.includes('<script>'));
        assert.ok(options.footer.includes('type="button"'));
        """
    )


def test_ledger_mobile_rows_keep_compact_flow_and_lower_category_badge() -> None:
    """手机账目行应保持紧凑横排，并把分类下移到账户信息高度。"""

    _run_ledger_client(
        r"""
        client.__ensureStyles();
        const css = __styleCalls.at(-1)[1];

        assert.match(css, /\.ledger-row \{\s*display: flex;/);
        assert.match(css, /\.ledger-row-main \{[\s\S]*?min-width: 0;[\s\S]*?overflow: hidden;/);
        assert.match(css, /\.ledger-row-title \{[\s\S]*?max-width: 100%;[\s\S]*?text-overflow: ellipsis;/);
        assert.ok(css.includes('.ledger-row { gap: 8px; padding: 9px 0; }'));
        assert.match(css, /\.ledger-category-badge \{[\s\S]*?transform: translateY\(8px\);/);
        assert.ok(!css.includes('grid-template-areas:'));
        """
    )


def test_ledger_loads_ignore_stale_and_destroyed_responses() -> None:
    """后发刷新必须胜出，销毁后的慢响应不得覆盖其他页面或重新订阅。"""

    _run_ledger_client(
        r"""
        const requests = [];
        __api.get = (path, params = {}) => new Promise((resolve, reject) => {
            requests.push({ path, params, resolve, reject });
        });
        const container = {
            innerHTML: '',
            querySelector: () => null,
        };
        const responseFor = (request, count) => {
            if (request.path === '/items') {
                return { data: { items: [], total: count } };
            }
            if (request.path === '/items/aggregate') {
                return { data: { income: count, expense: 0, balance: count, count } };
            }
            if (request.path === '/items/categories') {
                return { data: { categories: [`分类${count}`] } };
            }
            if (request.path === '/items/ledger/accounts') {
                return { data: { accounts: [`账户${count}`] } };
            }
            return { data: {} };
        };

        await assert.rejects(() => client.render(null), /账本页需要有效的 DOM 挂载容器/);
        const initial = client.render(container);
        assert.equal(requests.length, 5);
        const refresh = __dataChangeCallback();
        assert.equal(requests.length, 10);
        for (const request of requests.slice(5)) request.resolve(responseFor(request, 2));
        await refresh;
        const newestMarkup = container.innerHTML;
        assert.match(newestMarkup, /2 条账目/);

        for (const request of requests.slice(0, 5)) request.resolve(responseFor(request, 99));
        await initial;
        assert.equal(container.innerHTML, newestMarkup);
        client.destroy();
        assert.equal(__unsubscribeCount, 1);

        const requestStart = requests.length;
        const destroyedRender = client.render(container);
        const destroyedRequests = requests.slice(requestStart);
        assert.equal(destroyedRequests.length, 5);
        client.destroy();
        container.innerHTML = '<main>其他页面</main>';
        for (const request of destroyedRequests) request.resolve(responseFor(request, 88));
        await destroyedRender;
        assert.equal(container.innerHTML, '<main>其他页面</main>');
        assert.equal(__unsubscribeCount, 2);
        """
    )


def test_ledger_quick_add_validates_and_dispatches_once() -> None:
    """快速记账应严格校验金额/账户、防重复，并只广播一次刷新。"""

    _run_ledger_client(
        r"""
        const field = (value = '') => ({ value });
        const elements = new Map([
            ['#qa-transaction-type', { dataset: { value: 'expense' } }],
            ['#qa-amount', field('12yuan')],
            ['#qa-title', field('午饭')],
            ['#qa-category', field('餐饮')],
            ['#qa-account', field('微信')],
            ['#qa-counter', field('')],
            ['#qa-merchant', field('食堂')],
            ['#qa-date', field('2026-03-02')],
            ['#qa-submit', { disabled: false }],
        ]);
        const container = {
            querySelector: (selector) => elements.get(selector) || null,
        };
        client.__setLedgerTestState({ container });
        await client.__handleQuickAdd();
        assert.deepEqual(__toastCalls.at(-1), ['请填写有效金额', 'warning']);

        elements.get('#qa-amount').value = '12.50';
        const postCalls = [];
        let resolvePost;
        __api.post = (...args) => {
            postCalls.push(args);
            return new Promise((resolve) => { resolvePost = resolve; });
        };
        const first = client.__handleQuickAdd();
        const duplicate = client.__handleQuickAdd();
        assert.equal(postCalls.length, 1);
        assert.equal(elements.get('#qa-submit').disabled, true);
        resolvePost({ data: {} });
        await Promise.all([first, duplicate]);
        assert.deepEqual(postCalls, [[
            '/items',
            {
                type: 'ledger', transaction_type: 'expense', amount: 12.5,
                title: '午饭', ledger_category: '餐饮', ledger_date: '2026-03-02',
                account_name: '微信', counter_account_name: '', merchant: '食堂',
                currency: 'CNY',
            },
        ]]);
        assert.equal(elements.get('#qa-submit').disabled, false);
        assert.equal(elements.get('#qa-amount').value, '');
        assert.equal(__dispatchedEvents.length, 1);
        assert.deepEqual(__dispatchedEvents[0].detail, { type: 'ledger' });

        elements.get('#qa-transaction-type').dataset.value = 'transfer';
        elements.get('#qa-amount').value = '5';
        elements.get('#qa-title').value = '内部转账';
        elements.get('#qa-account').value = '现金';
        elements.get('#qa-counter').value = '现金';
        await client.__handleQuickAdd();
        assert.deepEqual(__toastCalls.at(-1), ['转出账户和转入账户不能相同', 'warning']);
        assert.equal(postCalls.length, 1);
        """
    )


def test_ledger_edit_delete_encode_ids_block_duplicates_and_dispatch_once() -> None:
    """编辑和删除应编码编号、互斥提交、清理非转账字段并单次广播。"""

    _run_ledger_client(
        r"""
        const buttons = new Map([
            ['#modal-cancel', { onclick: null }],
            ['#modal-delete', { onclick: null }],
            ['#modal-save', { onclick: null, disabled: false, click() { return this.onclick(); } }],
        ]);
        const form = { onsubmit: null };
        __modalContent = {
            querySelector(selector) {
                if (selector === '#ledger-edit-form') return form;
                return buttons.get(selector) || null;
            },
        };
        __formData = {
            title: '午饭', amount: 18.5, currency: '',
            transaction_type: 'expense', ledger_category: '餐饮',
            ledger_date: '2026-03-02', account_name: '',
            counter_account_name: '旧转入账户', merchant: '', remark: '',
        };
        const putCalls = [];
        let resolvePut;
        __api.put = (...args) => {
            putCalls.push(args);
            return new Promise((resolve) => { resolvePut = resolve; });
        };
        client.__openEditModal({
            id: 'ledger/a b', title: '旧记录', amount: 10,
            transaction_type: 'transfer', ledger_date: '2026-03-01',
            account_name: '现金', counter_account_name: '银行卡',
        });
        const saveButton = buttons.get('#modal-save');
        const first = saveButton.onclick();
        const duplicate = saveButton.onclick();
        assert.equal(putCalls.length, 1);
        assert.equal(saveButton.disabled, true);
        resolvePut({ data: {} });
        await Promise.all([first, duplicate]);
        assert.equal(putCalls[0][0], '/items/ledger%2Fa%20b');
        assert.equal(putCalls[0][1].currency, 'CNY');
        assert.equal(putCalls[0][1].account_name, '现金');
        assert.equal(putCalls[0][1].counter_account_name, '');
        assert.equal(saveButton.disabled, false);
        assert.equal(__dispatchedEvents.length, 1);
        assert.deepEqual(__dispatchedEvents[0].detail, { type: 'ledger' });
        assert.ok(__modalCalls[0][2].footer.includes('type="button"'));

        const deleteCalls = [];
        __api.delete = async (...args) => { deleteCalls.push(args); };
        client.__setLedgerTestState({
            items: [{ id: 'ledger/a b', title: '午饭' }],
        });
        await client.__handleDelete('ledger/a b');
        assert.deepEqual(deleteCalls, [['/items/ledger%2Fa%20b']]);
        assert.equal(__dispatchedEvents.length, 2);
        assert.deepEqual(__dispatchedEvents[1].detail, { type: 'ledger' });
        """
    )
