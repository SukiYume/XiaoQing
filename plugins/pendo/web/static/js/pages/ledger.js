import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { captureFormDraft, reopenWithDraft } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';
import { renderLedgerInsightsPanel } from '../components/ledger_insights.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import {
    finiteNumber,
    formatAmount as baseFormatAmount,
    isValidDateInput,
    nonNegativeInteger,
} from '../utils/format.js';
import { derivePresetRange, fetchItemRangeBounds, todayRangeKey } from '../utils/date_ranges.js';
import {
    bindEnterAction,
    bindFormSubmit,
    escapeHtml,
    subscribeDataChanges,
} from '../utils/ui.js';

// ── 常量与字段配置 ───────────────────────────────────────────────────────────

const PAGE_SIZE                = 50;
const AMOUNT_PATTERN           = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
const TRANSACTION_TYPES        = new Set(['expense', 'income', 'transfer']);
const TRANSACTION_TYPE_OPTIONS = Object.freeze([
    { value: 'expense', label: '支出' },
    { value: 'income', label: '收入' },
    { value: 'transfer', label: '转账' },
]);
const DATE_FILTER_OPTIONS = Object.freeze([
    { value: 'today', label: '今天' },
    { value: 'week', label: '本周' },
    { value: 'month', label: '本月' },
    { value: 'quarter', label: '本季' },
    { value: 'year', label: '今年' },
    { value: 'last_year', label: '去年' },
    { value: 'custom', label: '自定义' },
    { value: 'all', label: '全部' },
]);
const DATE_FILTER_LABELS = Object.freeze(
    Object.fromEntries(DATE_FILTER_OPTIONS.map(({ value, label }) => [value, label])),
);

const LEDGER_FIELDS = [
    { name: 'transaction_type', label: '类型', type: 'select', options: TRANSACTION_TYPE_OPTIONS },
    { name: 'amount', label: '金额', type: 'number', required: true, step: '0.01' },
    { name: 'currency', label: '币种', type: 'text', placeholder: 'CNY' },
    { name: 'title', label: '摘要', type: 'text', required: true },
    { name: 'ledger_category', label: '分类', type: 'text', placeholder: '其他' },
    { name: 'ledger_date', label: '日期', type: 'date' },
    { name: 'account_name', label: '账户', type: 'text', placeholder: '现金' },
    { name: 'counter_account_name', label: '转入账户', type: 'text', placeholder: '转账时填写' },
    { name: 'merchant', label: '商户/对方', type: 'text' },
    { name: 'remark', label: '备注', type: 'textarea' },
];

// ── 页面状态 ─────────────────────────────────────────────────────────────────

let _container             = null;
let _items                 = [];
let _total                 = 0;
let _page                  = 1;
let _dateFilter            = 'month';
let _currency              = 'CNY';
let _allRange              = null;
let _transactionTypeFilter = '';
let _categoryFilter        = '';
let _accountFilter         = '';
let _amountMin             = '';
let _amountMax             = '';
let _sortMode              = 'date';
let _customDateStart       = '';
let _customDateEnd         = '';
let _summaryData           = { income: 0, expense: 0, transfer: 0, balance: 0, count: 0 };
let _insightsData          = null;
let _allCategories         = [];
let _allAccounts           = [];
let _amountDebounceTimer   = null;
let _quickAddSaving        = false;
// 快速记账草稿：查询、分页、排序或数据变更会整页重绘，重绘前先记下用户正在填写的内容。
let _quickAddDraft          = null;
let _loadVersion            = 0;
let _unsubscribeDataChanges = null;

// ── 数据边界与筛选参数 ───────────────────────────────────────────────────────

function formatAmount(value, currency = _summaryData.currency || 'CNY') {
    return baseFormatAmount(value, currency);
}

function dateRangeForFilter(filter) {
    const range = derivePresetRange(filter, {
        today: todayRangeKey(),
        customStart: _customDateStart,
        customEnd: _customDateEnd,
        customFallback: '',
        allStart: _allRange?.start,
        allEnd: _allRange?.end,
    });
    if (!range.start || !range.end) return {};
    return {
        start_date: range.start,
        end_date: range.end,
    };
}

function compareModeForFilter(filter) {
    if (filter === 'year') return 'previous_year_to_date';
    if (filter === 'all') return 'none';
    return 'previous_period';
}

function parseAmountInput(value, { allowZero = true } = {}) {
    const text = String(value ?? '').trim();
    if (!AMOUNT_PATTERN.test(text)) return null;
    const amount = Number(text);
    if (!Number.isFinite(amount) || amount < 0 || (!allowZero && amount === 0)) return null;
    return amount;
}

function normalizedTransactionType(value) {
    return TRANSACTION_TYPES.has(value) ? value : 'expense';
}

function normalizeTextList(value, fallback = []) {
    if (!Array.isArray(value)) return [...fallback];
    return [...new Set(value.map((item) => String(item ?? '').trim()).filter(Boolean))];
}

function normalizeLedgerItem(value) {
    if (!value || typeof value !== 'object') return null;
    const ledgerDate = String(value.ledger_date ?? '').trim();
    return {
        id: String(value.id ?? ''),
        version: value.version,
        transaction_type: normalizedTransactionType(value.transaction_type),
        amount: Math.max(0, finiteNumber(value.amount)),
        currency: String(value.currency ?? 'CNY'),
        title: String(value.title ?? ''),
        ledger_category: String(value.ledger_category ?? ''),
        ledger_date: isValidDateInput(ledgerDate) ? ledgerDate : '',
        account_name: String(value.account_name ?? ''),
        counter_account_name: String(value.counter_account_name ?? ''),
        merchant: String(value.merchant ?? ''),
        remark: String(value.remark ?? ''),
    };
}

function normalizeSummary(value) {
    const raw = value && typeof value === 'object' ? value : {};
    return {
        currency: String(raw.currency || 'CNY'),
        income: Math.max(0, finiteNumber(raw.income)),
        expense: Math.max(0, finiteNumber(raw.expense)),
        transfer: Math.max(0, finiteNumber(raw.transfer)),
        balance: finiteNumber(raw.balance),
        count: nonNegativeInteger(raw.count),
    };
}

function currentFilterParams() {
    const params = { ...dateRangeForFilter(_dateFilter), currency: _currency };
    if (_transactionTypeFilter) params.transaction_type = _transactionTypeFilter;
    if (_categoryFilter) params.category = _categoryFilter;
    if (_accountFilter) params.account_name = _accountFilter;
    const amountMin = parseAmountInput(_amountMin);
    const amountMax = parseAmountInput(_amountMax);
    if (amountMin !== null) params.amount_min = amountMin;
    if (amountMax !== null) params.amount_max = amountMax;
    return params;
}

function groupByDate(items) {
    const groups = new Map();
    for (const item of Array.isArray(items) ? items : []) {
        const date = item.ledger_date || '未知日期';
        if (!groups.has(date)) groups.set(date, []);
        groups.get(date).push(item);
    }
    return [...groups.entries()]
        .sort(([left], [right]) => right.localeCompare(left))
        .map(([date, dateItems]) => ({ date, items: dateItems }));
}

// ── API 读取 ─────────────────────────────────────────────────────────────────

async function fetchItems(page) {
    const params = {
        ...currentFilterParams(),
        type: 'ledger',
        date_field: 'ledger_date',
        sort: _sortMode === 'amount' ? 'amount' : 'ledger_date',
        order: 'desc',
        page: Math.max(1, nonNegativeInteger(page)),
        page_size: PAGE_SIZE,
    };
    const res = await api.get('/items', params);
    const items = (Array.isArray(res?.data?.items) ? res.data.items : []).map(normalizeLedgerItem).filter(Boolean);
    return {
        items,
        total: Math.max(items.length, nonNegativeInteger(res?.data?.total)),
    };
}

async function fetchAggregate() {
    const params = { ...currentFilterParams(), type: 'ledger', date_field: 'ledger_date' };
    const res    = await api.get('/items/aggregate', params);
    return normalizeSummary(res?.data);
}

async function fetchCategories() {
    try {
        const res = await api.get('/items/categories', { type: 'ledger' });
        return normalizeTextList(res?.data?.categories);
    } catch {
        return [];
    }
}

async function fetchAccounts() {
    try {
        const res = await api.get('/items/ledger/accounts');
        return normalizeTextList(res?.data?.accounts, ['现金']);
    } catch {
        return ['现金'];
    }
}

async function fetchInsights() {
    const params = currentFilterParams();
    params.compare_mode = compareModeForFilter(_dateFilter);
    const res = await api.get('/stats/ledger/insights', params);
    return res?.data && typeof res.data === 'object' ? res.data : null;
}

// ── 页面样式 ─────────────────────────────────────────────────────────────────

// ── render ────────────────────────────────────────────────────────────────────

function renderSummaryCards() {
    const { income, expense, transfer = 0, balance } = _summaryData;
    const balanceClass = balance >= 0 ? 'ledger-amount-income' : 'ledger-amount-expense';
    return `
        <div class="ledger-summary-cards">
            <div class="ledger-summary-card income">
                <span class="ledger-summary-icon" aria-hidden="true"><span class="pixel-icon" data-icon="income"></span></span>
                <div class="ledger-summary-label">收入</div>
                <div class="ledger-summary-value ledger-amount-income">${formatAmount(income)}</div>
            </div>
            <div class="ledger-summary-card expense">
                <span class="ledger-summary-icon" aria-hidden="true"><span class="pixel-icon" data-icon="expense"></span></span>
                <div class="ledger-summary-label">支出</div>
                <div class="ledger-summary-value ledger-amount-expense">${formatAmount(expense)}</div>
            </div>
            <div class="ledger-summary-card balance">
                <span class="ledger-summary-icon" aria-hidden="true"><span class="pixel-icon" data-icon="balance"></span></span>
                <div class="ledger-summary-label">结余</div>
                <div class="ledger-summary-value ${balanceClass}">${formatAmount(balance)}</div>
            </div>
            <div class="ledger-summary-card transfer">
                <span class="ledger-summary-icon" aria-hidden="true"><span class="pixel-icon" data-icon="transfer"></span></span>
                <div class="ledger-summary-label">转账</div>
                <div class="ledger-summary-value ledger-amount-transfer">${formatAmount(transfer)}</div>
            </div>
        </div>`;
}

function captureQuickAddDraft() {
    const root = _container?.querySelector('#ledger-quick-add');
    if (!root) return;
    const read = (selector) => String(root.querySelector(selector)?.value ?? '');
    _quickAddDraft = {
        type: root.querySelector('#qa-transaction-type')?.dataset.value || 'expense',
        amount: read('#qa-amount'),
        title: read('#qa-title'),
        category: read('#qa-category'),
        account: read('#qa-account'),
        counter: read('#qa-counter'),
        merchant: read('#qa-merchant'),
        date: read('#qa-date'),
    };
}

function renderQuickAdd() {
    const today      = todayRangeKey();
    const draft      = _quickAddDraft || {};
    const keep       = (key) => escapeHtml(draft[key] ?? '');
    const typeSelect = renderCustomSelect({
        id: 'qa-transaction-type',
        options: TRANSACTION_TYPE_OPTIONS,
        selected: draft.type || 'expense',
        className: 'pselect-block pselect-theme-ledger ledger-qa-transaction-type',
        labelledBy: 'qa-transaction-type-label',
    });
    return `
        <div class="ledger-quick-add" id="ledger-quick-add">
            <div class="ledger-qa-field ledger-qa-field--type">
                <label class="ledger-qa-label" id="qa-transaction-type-label">方向</label>
                ${typeSelect}
            </div>
            <div class="ledger-qa-field ledger-qa-field--amount">
                <label class="ledger-qa-label" for="qa-amount">金额 · ${escapeHtml(_currency)}</label>
                <input type="number" class="ledger-qa-amount" id="qa-amount" value="${keep('amount')}" placeholder="0.00" step="0.01" min="0" aria-label="金额">
            </div>
            <div class="ledger-qa-field ledger-qa-field--title">
                <label class="ledger-qa-label" for="qa-title">摘要</label>
                <input type="text" class="ledger-qa-title" id="qa-title" value="${keep('title')}" placeholder="餐饮、交通、工资..." aria-label="摘要">
            </div>
            <div class="ledger-qa-field ledger-qa-field--category">
                <label class="ledger-qa-label" for="qa-category">分类</label>
                <input type="text" class="ledger-qa-category" id="qa-category" value="${keep('category')}" placeholder="其他" aria-label="分类">
            </div>
            <div class="ledger-qa-field ledger-qa-field--account">
                <label class="ledger-qa-label" for="qa-account">账户</label>
                <input type="text" class="ledger-qa-account" id="qa-account" value="${keep('account')}" placeholder="现金" aria-label="账户">
            </div>
            <div class="ledger-qa-field ledger-qa-field--counter">
                <label class="ledger-qa-label" for="qa-counter">转入账户</label>
                <input type="text" class="ledger-qa-counter" id="qa-counter" value="${keep('counter')}" placeholder="仅转账填写" aria-label="转入账户">
            </div>
            <div class="ledger-qa-field ledger-qa-field--merchant">
                <label class="ledger-qa-label" for="qa-merchant">商户/对方</label>
                <input type="text" class="ledger-qa-merchant" id="qa-merchant" value="${keep('merchant')}" placeholder="可选" aria-label="商户或对方">
            </div>
            <div class="ledger-qa-field ledger-qa-field--date">
                <label class="ledger-qa-label" for="qa-date">日期</label>
                <input type="text" class="ledger-qa-date" id="qa-date" value="${escapeHtml(draft.date || today)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="记账日期">
            </div>
            <div class="ledger-qa-action">
                <button type="button" class="ledger-qa-submit" id="qa-submit">+ 记录</button>
            </div>
        </div>`;
}

function renderFilterBar() {
    const typeOptions = [{ value: '', label: '全部类型' }, ...TRANSACTION_TYPE_OPTIONS];
    const catOptions  = [
        { value: '', label: '全部分类' },
        ..._allCategories.map((category) => ({ value: category, label: category })),
    ];
    const accountOptions = [
        { value: '', label: '全部账户' },
        ..._allAccounts.map((account) => ({ value: account, label: account })),
    ];

    return `
        <div class="ledger-filter-bar" id="ledger-filter-bar">
            <div class="ledger-filter-item ledger-filter-item--currency">
                <label for="filter-currency">币种</label>
                <div class="ledger-filter-controls">
                    <input id="filter-currency" class="ledger-custom-date-input" value="${escapeHtml(_currency)}" maxlength="3" size="4" aria-label="账本币种" placeholder="CNY">
                    <button type="button" id="filter-currency-apply" class="btn btn-secondary">切换</button>
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--date">
                <label id="filter-date-label">时段</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-date', options: DATE_FILTER_OPTIONS, selected: _dateFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-date', labelledBy: 'filter-date-label' })}
                </div>
            </div>
            <div class="ledger-filter-range" id="filter-custom-range" ${_dateFilter === 'custom' ? '' : 'hidden'}>
                <input type="text" class="ledger-custom-date-input" id="filter-date-start" value="${escapeHtml(_customDateStart)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="筛选开始日期">
                <span class="ledger-filter-range-sep">至</span>
                <input type="text" class="ledger-custom-date-input" id="filter-date-end" value="${escapeHtml(_customDateEnd)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="筛选结束日期">
                <button type="button" class="ledger-range-apply" id="filter-date-apply">应用</button>
            </div>
            <div class="ledger-filter-item ledger-filter-item--transaction-type">
                <label id="filter-transaction-type-label">类型</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-transaction-type', options: typeOptions, selected: _transactionTypeFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-transaction-type', labelledBy: 'filter-transaction-type-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--account">
                <label id="filter-account-label">账户</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-account', options: accountOptions, selected: _accountFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-account', labelledBy: 'filter-account-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--category">
                <label id="filter-category-label">分类</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-category', options: catOptions, selected: _categoryFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-category', labelledBy: 'filter-category-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--amount">
                <label>金额</label>
                <div class="ledger-filter-controls">
                    <input type="number" class="ledger-amount-input" id="filter-amount-min"
                        aria-label="最小金额"
                        placeholder="最小" min="0" step="0.01" value="${escapeHtml(_amountMin)}">
                    <span class="ledger-amount-separator">~</span>
                    <input type="number" class="ledger-amount-input" id="filter-amount-max"
                        aria-label="最大金额"
                        placeholder="最大" min="0" step="0.01" value="${escapeHtml(_amountMax)}">
                </div>
            </div>
        </div>`;
}

function renderItemRow(item) {
    const txType      = normalizedTransactionType(item.transaction_type);
    const isIncome    = txType === 'income';
    const isTransfer  = txType === 'transfer';
    const dirIcon     = isTransfer ? '↔' : isIncome ? '↑' : '↓';
    const amountClass = isTransfer
        ? 'ledger-amount-transfer'
        : isIncome
          ? 'ledger-amount-income'
          : 'ledger-amount-expense';
    const amountSign = isTransfer ? '↔ ' : isIncome ? '+' : '-';
    const meta       = _sortMode === 'amount' && item.ledger_date
            ? `<span class="ledger-row-meta">${escapeHtml(item.ledger_date)}</span>`
            : '';
    const catBadge = item.ledger_category
        ? `<span class="badge ledger-category-badge">${escapeHtml(item.ledger_category)}</span>`
        : '';
    const accountText =
        isTransfer && item.counter_account_name
            ? `${item.account_name || '现金'} → ${item.counter_account_name}`
            : item.account_name || '现金';
    const accountMeta = `<span class="ledger-row-meta">${escapeHtml(accountText)}${item.merchant ? ' · ' + escapeHtml(item.merchant) : ''}</span>`;
    const itemId = String(item.id ?? '');
    const escapedId = escapeHtml(itemId);
    const title = item.title || '(无摘要)';
    return `
        <div class="ledger-row">
            <span class="ledger-dir-icon" aria-hidden="true">${dirIcon}</span>
            <button type="button" class="ledger-row-main" data-open-ledger="${escapedId}" aria-label="查看账目：${escapeHtml(title)}" ${itemId ? '' : 'disabled'}>
                <span class="ledger-row-title" title="${escapeHtml(item.title || '')}">${escapeHtml(title)}</span>
                ${meta}
                ${accountMeta}
            </button>
            ${catBadge}
            <span class="ledger-row-amount ${amountClass}">${amountSign}${formatAmount(item.amount, item.currency)}</span>
            <div class="ledger-row-actions">
                <button type="button" class="btn btn-icon btn-sm btn-edit-ledger" data-id="${escapedId}" aria-label="编辑：${escapeHtml(title)}" ${itemId ? '' : 'disabled'}><span class="pixel-icon" data-icon="edit" aria-hidden="true"></span></button>
                <button type="button" class="btn btn-icon btn-sm btn-delete-ledger" data-id="${escapedId}" aria-label="删除：${escapeHtml(title)}" ${itemId ? '' : 'disabled'}><span class="pixel-icon" data-icon="trash" aria-hidden="true"></span></button>
            </div>
        </div>`;
}

function renderListToolbar() {
    const sortHint = _sortMode === 'amount' ? '当前筛选条件下按金额从高到低' : '当前筛选条件下按日期从新到旧';
    return `
        <div class="ledger-list-toolbar">
            <div class="ledger-list-toolbar-copy">
                <strong>账目明细</strong>
                <span>${sortHint}</span>
            </div>
            <div class="ledger-sort-toggle" role="group" aria-label="记账排序">
                <button class="ledger-sort-btn${_sortMode === 'date' ? ' is-active' : ''}" id="ledger-sort-date" type="button" aria-pressed="${_sortMode === 'date'}">按时间</button>
                <button class="ledger-sort-btn${_sortMode === 'amount' ? ' is-active' : ''}" id="ledger-sort-amount" type="button" aria-pressed="${_sortMode === 'amount'}">按金额</button>
            </div>
        </div>`;
}

function renderList(items) {
    if (!Array.isArray(items) || items.length === 0) return '<div class="ledger-empty">暂无记录</div>';
    if (_sortMode === 'amount') {
        return `<div class="ledger-flat-list">${items.map(renderItemRow).join('')}</div>`;
    }
    const groups = groupByDate(items);
    return groups
        .map(
            (g) => `
        <div class="ledger-date-group">
            <div class="ledger-date-group-header">${escapeHtml(g.date)}</div>
            ${g.items.map(renderItemRow).join('')}
        </div>`,
        )
        .join('');
}

function renderPage() {
    if (!_container) return;
    captureQuickAddDraft();

    _container.innerHTML = `
        <div class="ledger-page">
            <section class="ledger-hero pendo-page-header">
                <div class="ledger-hero-copy">
                    <span class="eyebrow"><i></i>LEDGER</span>
                    <h2>账本</h2>
                    <p>${DATE_FILTER_LABELS[_dateFilter] || '当前范围'} · ${_total} 条账目 · ${escapeHtml(_currency)}</p>
                </div>
                <div class="ledger-page-header">
                    <button type="button" class="btn btn-primary" id="ledger-add-top">＋ 记一笔</button>
                </div>
            </section>
            <div class="ledger-section-stack">
                <details class="pendo-analysis" id="ledger-analysis" open>
                    <summary>收支分析</summary>
                    <div class="ledger-analysis-body">${renderSummaryCards()}${renderLedgerInsightsPanel(_insightsData)}</div>
                </details>
                <section class="ledger-controls-grid">
                    <section class="ledger-panel ledger-filter-panel">
                        <div class="ledger-panel-head"><h3>查询</h3></div>
                        <div class="ledger-panel-body">${renderFilterBar()}</div>
                    </section>
                    <details class="ledger-panel" id="ledger-compose" open>
                        <summary class="ledger-compose-toggle">记一笔</summary>
                        <div class="ledger-panel-body">${renderQuickAdd()}</div>
                    </details>
                </section>
                <div class="ledger-records-card">
                    ${renderListToolbar()}
                    <div id="ledger-list">${renderList(_items)}</div>
                    <div id="ledger-pagination" class="ledger-pagination"></div>
                </div>
            </div>
        </div>`;

    const paginationEl = _container.querySelector('#ledger-pagination');
    if (paginationEl) {
        renderPagination(paginationEl, {
            page: _page,
            pageSize: PAGE_SIZE,
            total: _total,
            onChange: async (newPage) => {
                _page = newPage;
                await loadAndRender();
            },
        });
    }

    attachListeners();
}

// ── 页面交互 ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;
    const root      = _container;
    const addButton = root.querySelector('#ledger-add-top');
    if (addButton) addButton.onclick = () => {
        const editor = root.querySelector('#ledger-compose');
        if (editor) editor.open = true;
        root.querySelector('#qa-amount')?.focus();
    };
    const currencyInput = root.querySelector('#filter-currency');
    const applyCurrency = async () => {
        const currency = String(currencyInput?.value || '').trim().toUpperCase();
        if (!/^[A-Z]{3}$/.test(currency)) {
            showToast('请输入三个字母的币种代码，如 CNY、USD', 'warning');
            return;
        }
        _currency = currency;
        _allRange = null;
        _page = 1;
        await loadAndRender();
    };
    bindEnterAction(currencyInput, applyCurrency);
    const currencyApply = root.querySelector('#filter-currency-apply');
    if (currencyApply) currencyApply.onclick = applyCurrency;

    const qaSubmit = root.querySelector('#qa-submit');
    if (qaSubmit) qaSubmit.addEventListener('click', handleQuickAdd);

    for (const id of ['qa-amount', 'qa-title', 'qa-category', 'qa-account', 'qa-counter', 'qa-merchant', 'qa-date']) {
        const input = root.querySelector(`#${id}`);
        if (!input) continue;
        input.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' || event.isComposing) return;
            event.preventDefault();
            void handleQuickAdd();
        });
    }

    initCustomSelects(root, {
        'filter-date': async (value) => {
            _dateFilter = DATE_FILTER_LABELS[value] ? value : 'month';
            _page = 1;
            if (_dateFilter === 'custom' && (!_customDateStart || !_customDateEnd)) {
                const range = dateRangeForFilter('month');
                _customDateStart = range.start_date;
                _customDateEnd = range.end_date;
            }
            await loadAndRender();
        },
        'filter-transaction-type': async (value) => {
            _transactionTypeFilter = TRANSACTION_TYPES.has(value) ? value : '';
            _page = 1;
            await loadAndRender();
        },
        'filter-category': async (value) => {
            _categoryFilter = String(value ?? '');
            _page = 1;
            await loadAndRender();
        },
        'filter-account': async (value) => {
            _accountFilter = String(value ?? '');
            _page = 1;
            await loadAndRender();
        },
    });

    const dateStart            = root.querySelector('#filter-date-start');
    const dateEnd              = root.querySelector('#filter-date-end');
    const dateApply            = root.querySelector('#filter-date-apply');
    const applyCustomDateRange = async () => {
        const nextStart = dateStart?.value.trim() || '';
        const nextEnd   = dateEnd?.value.trim() || '';
        if (!isValidDateInput(nextStart) || !isValidDateInput(nextEnd)) {
            showToast('请输入有效日期，格式为 YYYY-MM-DD', 'warning');
            return;
        }
        if (nextStart > nextEnd) {
            showToast('开始日期不能晚于结束日期', 'warning');
            return;
        }
        _customDateStart = nextStart;
        _customDateEnd = nextEnd;
        _page = 1;
        await loadAndRender();
    };
    bindEnterAction(dateStart, applyCustomDateRange);
    bindEnterAction(dateEnd, applyCustomDateRange);
    if (dateApply) dateApply.addEventListener('click', applyCustomDateRange);

    for (const id of ['filter-amount-min', 'filter-amount-max']) {
        const input = root.querySelector(`#${id}`);
        if (!input) continue;
        input.addEventListener('input', () => {
            clearTimeout(_amountDebounceTimer);
            _amountDebounceTimer = setTimeout(async () => {
                if (_container !== root) return;
                const nextMin   = root.querySelector('#filter-amount-min')?.value.trim() || '';
                const nextMax   = root.querySelector('#filter-amount-max')?.value.trim() || '';
                const minAmount = nextMin ? parseAmountInput(nextMin) : null;
                const maxAmount = nextMax ? parseAmountInput(nextMax) : null;
                if ((nextMin && minAmount === null) || (nextMax && maxAmount === null)) {
                    showToast('请输入有效的非负金额', 'warning');
                    return;
                }
                if (minAmount !== null && maxAmount !== null && minAmount > maxAmount) {
                    showToast('最小金额不能大于最大金额', 'warning');
                    return;
                }
                _amountMin = nextMin;
                _amountMax = nextMax;
                _page = 1;
                await loadAndRender();
            }, 600);
        });
    }

    for (const id of ['ledger-sort-date', 'ledger-sort-amount']) {
        const btn = root.querySelector(`#${id}`);
        if (!btn) continue;
        btn.addEventListener('click', async () => {
            const nextMode = id === 'ledger-sort-amount' ? 'amount' : 'date';
            if (_sortMode === nextMode) return;
            _sortMode = nextMode;
            _page = 1;
            await loadAndRender();
        });
    }

    const list = root.querySelector('#ledger-list');
    if (list) {
        list.addEventListener('click', async (event) => {
            const editBtn   = event.target.closest('.btn-edit-ledger');
            const deleteBtn = event.target.closest('.btn-delete-ledger');
            const openBtn   = event.target.closest('[data-open-ledger]');
            if (editBtn) {
                const item = _items.find((entry) => entry.id === editBtn.dataset.id);
                if (item) openEditModal(item);
            } else if (deleteBtn) {
                await handleDelete(deleteBtn.dataset.id);
            } else if (openBtn) {
                const item = _items.find((entry) => entry.id === openBtn.dataset.openLedger);
                if (item) openDetailModal(item);
            }
        });
    }
}

// ── 快速记账 ─────────────────────────────────────────────────────────────────

async function handleQuickAdd() {
    if (!_container || _quickAddSaving) return;
    const root          = _container;
    const typeSelect    = root.querySelector('#qa-transaction-type');
    const amountInput   = root.querySelector('#qa-amount');
    const titleInput    = root.querySelector('#qa-title');
    const categoryInput = root.querySelector('#qa-category');
    const accountInput  = root.querySelector('#qa-account');
    const counterInput  = root.querySelector('#qa-counter');
    const merchantInput = root.querySelector('#qa-merchant');
    const dateInput     = root.querySelector('#qa-date');
    const submitButton  = root.querySelector('#qa-submit');
    if (
        !amountInput ||
        !titleInput ||
        !categoryInput ||
        !accountInput ||
        !counterInput ||
        !merchantInput ||
        !dateInput ||
        !submitButton
    ) {
        showToast('快速记账表单未正确加载', 'error');
        return;
    }

    const transactionType = normalizedTransactionType(typeSelect?.dataset.value);
    const amount = parseAmountInput(amountInput.value, { allowZero: false });
    const title = titleInput.value.trim();
    const category = categoryInput.value.trim() || (transactionType === 'transfer' ? '转账' : '其他');
    const account = accountInput.value.trim() || '现金';
    const counter = counterInput.value.trim();
    const merchant = merchantInput.value.trim();
    const ledgerDate = dateInput.value.trim();

    if (amount === null) {
        showToast('请填写有效金额', 'warning');
        return;
    }
    if (!title) {
        showToast('请填写摘要', 'warning');
        return;
    }
    if (!isValidDateInput(ledgerDate)) {
        showToast('请填写有效日期，格式为 YYYY-MM-DD', 'warning');
        return;
    }
    if (transactionType === 'transfer' && !counter) {
        showToast('转账需要填写转入账户', 'warning');
        return;
    }
    if (transactionType === 'transfer' && account === counter) {
        showToast('转出账户和转入账户不能相同', 'warning');
        return;
    }

    _quickAddSaving = true;
    submitButton.disabled = true;
    const submitted = { amount: amountInput.value, title: titleInput.value, merchant: merchantInput.value };
    try {
        await api.post('/items', {
            type: 'ledger',
            transaction_type: transactionType,
            amount,
            title,
            ledger_category: category,
            ledger_date: ledgerDate,
            account_name: account,
            counter_account_name: counter,
            merchant,
            currency: _currency,
        });
        showToast('记录已添加', 'success');
        // 只清理本次已提交的字段；请求期间用户接着输入的新草稿保留（输入框可能已随重绘替换）。
        const live = _container?.querySelector('#ledger-quick-add');
        for (const [key, selector, original] of [
            ['amount', '#qa-amount', amountInput],
            ['title', '#qa-title', titleInput],
            ['merchant', '#qa-merchant', merchantInput],
        ]) {
            const input = live?.querySelector(selector) || original;
            if (input.value === submitted[key]) input.value = '';
        }
        captureQuickAddDraft();
        _page = 1;
        window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'ledger' } }));
    } catch (err) {
        showToast(`添加失败：${err?.message || '未知错误'}`, 'error');
    } finally {
        _quickAddSaving = false;
        submitButton.disabled = false;
    }
}

// ── 详情弹窗 ─────────────────────────────────────────────────────────────────

export function openDetailModal(item) {
    const normalizedItem = normalizeLedgerItem(item);
    if (!normalizedItem) {
        showToast('无法查看无效的账目记录', 'error');
        return;
    }
    const txType      = normalizedItem.transaction_type;
    const isIncome    = txType === 'income';
    const isTransfer  = txType === 'transfer';
    const amountClass = isTransfer
        ? 'ledger-amount-transfer'
        : isIncome
          ? 'ledger-amount-income'
          : 'ledger-amount-expense';
    const amountSign     = isTransfer ? '↔ ' : isIncome ? '+' : '-';
    const directionLabel = isTransfer ? '转账' : isIncome ? '收入' : '支出';
    const accountText    = isTransfer && normalizedItem.counter_account_name
            ? `${normalizedItem.account_name || '现金'} → ${normalizedItem.counter_account_name}`
            : normalizedItem.account_name || '现金';
    const rows = [
        ['摘要', normalizedItem.title],
        ['分类', normalizedItem.ledger_category],
        ['日期', normalizedItem.ledger_date],
        ['账户', accountText],
        ['商户/对方', normalizedItem.merchant],
        ['币种', normalizedItem.currency],
        ['备注', normalizedItem.remark],
    ].filter(([, value]) => value);

    const rowsHtml = rows
        .map(
            ([label, value]) => `
        <div class="ledger-detail-row">
            <span class="ledger-detail-label">${escapeHtml(label)}</span>
            <span class="ledger-detail-value">${escapeHtml(value)}</span>
        </div>`,
        )
        .join('');

    const body = `
        <div class="ledger-detail-shell">
            <div class="ledger-detail-head">
                <span class="ledger-detail-amount ${amountClass}">${amountSign}${formatAmount(normalizedItem.amount, normalizedItem.currency)}</span>
                <span class="ledger-detail-type ${amountClass}">${directionLabel}</span>
            </div>
            <div class="ledger-detail-rows">${rowsHtml}</div>
        </div>`;

    const footer = `
        <button type="button" class="btn btn-secondary" id="detail-close">关闭</button>
        <button type="button" class="btn btn-primary" id="detail-edit" ${normalizedItem.id ? '' : 'disabled'}>编辑</button>`;

    const content = showModal(normalizedItem.title || '记录详情', safeHtml(body), {
        footer: safeHtml(footer),
    });
    content.querySelector('#detail-close').onclick = closeModal;
    content.querySelector('#detail-edit').onclick = () => {
        closeModal(content);
        openEditModal(normalizedItem);
    };
}

// ── 编辑弹窗 ─────────────────────────────────────────────────────────────────

function openEditModal(existing) {
    const normalizedItem = normalizeLedgerItem(existing);
    if (!normalizedItem?.id) {
        showToast('无法编辑缺少编号的账目记录', 'error');
        return;
    }
    const fields = LEDGER_FIELDS.map((field) => ({
        ...field,
        value: normalizedItem[field.name] ?? field.value ?? '',
    }));

    const bodyHTML = `<form id="ledger-edit-form">${buildFormHTML(fields)}</form>`;
    const footer   = `
        <button type="button" class="btn btn-danger btn-sm ledger-modal-delete" id="modal-delete">删除</button>
        <button type="button" class="btn btn-secondary" id="modal-cancel">取消</button>
        <button type="button" class="btn btn-primary" id="modal-save">保存</button>`;

    const content = showModal('编辑记录', safeHtml(bodyHTML), { footer: safeHtml(footer) });
    initFormInteractions(content);

    const form       = content.querySelector('#ledger-edit-form');
    const saveButton = content.querySelector('#modal-save');
    bindFormSubmit(form, saveButton);
    content.querySelector('#modal-cancel').onclick = closeModal;
    content.querySelector('#modal-delete').onclick = async () => {
        const draft  = captureFormDraft(form);
        const reopen = () => reopenWithDraft(() => openEditModal(existing), draft, '#ledger-edit-form');
        closeModal(content);
        await handleDelete(normalizedItem.id, reopen);
    };
    let saving = false;
    saveButton.onclick = async () => {
        if (saving) return;
        if (!form) {
            showToast('账目编辑器未正确加载', 'error');
            return;
        }
        const data = getFormData(form);
        if (!data.title) {
            showToast('请填写摘要', 'warning');
            return;
        }
        const amount = finiteNumber(data.amount);
        if (amount <= 0) {
            showToast('请填写有效金额', 'warning');
            return;
        }
        data.amount = amount;
        data.transaction_type = normalizedTransactionType(data.transaction_type);
        if (!isValidDateInput(data.ledger_date)) {
            showToast('请填写有效日期，格式为 YYYY-MM-DD', 'warning');
            return;
        }
        if (!data.currency) data.currency = 'CNY';
        if (!data.account_name) data.account_name = '现金';
        if (data.transaction_type === 'transfer' && !data.counter_account_name) {
            showToast('转账需要填写转入账户', 'warning');
            return;
        }
        if (data.transaction_type === 'transfer' && data.account_name === data.counter_account_name) {
            showToast('转出账户和转入账户不能相同', 'warning');
            return;
        }
        if (data.transaction_type !== 'transfer') data.counter_account_name = '';

        saving = true;
        saveButton.disabled = true;
        try {
            await api.put(`/items/${encodeURIComponent(normalizedItem.id)}`, { ...data, version: normalizedItem.version });
            _currency = data.currency.trim().toUpperCase();
            showToast('记录已更新', 'success');
            closeModal(content);
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'ledger' } }));
        } catch (err) {
            showToast(`更新失败：${err?.message || '未知错误'}`, 'error');
        } finally {
            saving = false;
            saveButton.disabled = false;
        }
    };
}

// ── 删除记录 ─────────────────────────────────────────────────────────────────

async function handleDelete(id, reopenEditor = null) {
    const normalizedId = String(id ?? '');
    if (!normalizedId) {
        showToast('无法删除缺少编号的账目记录', 'error');
        return;
    }
    const item      = _items.find((entry) => entry.id === normalizedId);
    const label     = item?.title?.trim() || '这条记录';
    const confirmed = await showConfirmModal({
        title: '删除记录',
        message: `确定要删除“${label}”吗？删除后这条账目会从当前列表和统计中移除。`,
        confirmText: '删除',
        cancelText: reopenEditor ? '返回编辑' : '取消',
        tone: 'danger',
    });
    if (!confirmed) {
        if (reopenEditor) await reopenEditor();
        return;
    }
    try {
        await api.delete(`/items/${encodeURIComponent(normalizedId)}`);
        showToast('记录已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'ledger' } }));
    } catch (err) {
        showToast(`删除失败：${err?.message || '未知错误'}`, 'error');
        if (reopenEditor) await reopenEditor();
    }
}

// ── 数据加载与生命周期 ───────────────────────────────────────────────────────

async function loadAndRender(refreshCategories = false) {
    if (!_container) return;
    const container      = _container;
    const requestVersion = ++_loadVersion;
    try {
        if (_dateFilter === 'all') {
            const filters = currentFilterParams();
            delete filters.start_date;
            delete filters.end_date;
            const bounds = await fetchItemRangeBounds(api, { type: 'ledger', currency: _currency, filters });
            if (_container !== container || requestVersion !== _loadVersion) return;
            _allRange = bounds;
        }
        const fetches = [fetchItems(_page), fetchAggregate(), fetchInsights().catch(() => null)];
        if (refreshCategories) {
            fetches.push(fetchCategories());
            fetches.push(fetchAccounts());
        }
        const results = await Promise.all(fetches);
        if (_container !== container || requestVersion !== _loadVersion) return;
        const maxPage = Math.max(1, Math.ceil(results[0].total / PAGE_SIZE));
        if (_page > maxPage) {
            _page = maxPage;
            await loadAndRender(refreshCategories);
            return;
        }
        _items = results[0].items;
        _total = results[0].total;
        _summaryData = results[1];
        _insightsData = results[2];
        if (refreshCategories) {
            _allCategories = results[3];
            _allAccounts = results[4];
        }
    } catch (err) {
        if (_container !== container || requestVersion !== _loadVersion) return;
        _items = [];
        _total = 0;
        _summaryData = { income: 0, expense: 0, transfer: 0, balance: 0, count: 0 };
        _insightsData = null;
        showToast(`加载账本失败：${err?.message || '未知错误'}`, 'error');
    } finally {
        if (_container !== container || requestVersion !== _loadVersion) return;
        renderPage();
    }
}

// ── 页面入口 ─────────────────────────────────────────────────────────────────

export async function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('账本页需要有效的 DOM 挂载容器');
    }
    _loadVersion += 1;
    clearTimeout(_amountDebounceTimer);
    _amountDebounceTimer = null;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = container;
    _items = [];
    _total = 0;
    _page = 1;
    _allRange = null;
    _sortMode = 'date';
    _summaryData = { income: 0, expense: 0, transfer: 0, balance: 0, count: 0 };
    _insightsData = null;
    _allCategories = [];
    _allAccounts = ['现金'];
    _quickAddSaving = false;
    _quickAddDraft = null;

    renderPage();
    _unsubscribeDataChanges = subscribeDataChanges('ledger', () => loadAndRender(true));
    await loadAndRender(true);
}

export function destroy() {
    _loadVersion += 1;
    clearTimeout(_amountDebounceTimer);
    _amountDebounceTimer = null;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
    _items = [];
    _total = 0;
    _quickAddSaving = false;
}
