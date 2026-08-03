import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';
import { renderLedgerInsightsPanel } from '../components/ledger_insights.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import {
    finiteNumber,
    formatAmount,
    isValidDateInput,
    nonNegativeInteger,
} from '../utils/format.js';
import { derivePresetRange, todayRangeKey } from '../utils/date_ranges.js';
import {
    bindEnterAction,
    bindFormSubmit,
    BREAKPOINTS,
    escapeHtml,
    injectStyles,
    mediaMax,
    pageShellCss,
    subscribeDataChanges,
} from '../utils/ui.js';

// ── 常量与字段配置 ───────────────────────────────────────────────────────────

const PAGE_SIZE = 50;
const CSS_ID = 'pendo-ledger-styles';
const AMOUNT_PATTERN = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
const TRANSACTION_TYPES = new Set(['expense', 'income', 'transfer']);
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

let _container = null;
let _items = [];
let _total = 0;
let _page = 1;
let _dateFilter = 'month';
let _transactionTypeFilter = '';
let _categoryFilter = '';
let _accountFilter = '';
let _amountMin = '';
let _amountMax = '';
let _sortMode = 'date';
let _customDateStart = '';
let _customDateEnd = '';
let _summaryData = { income: 0, expense: 0, transfer: 0, balance: 0, count: 0 };
let _insightsData = null;
let _allCategories = [];
let _allAccounts = [];
let _amountDebounceTimer = null;
let _quickAddSaving = false;
let _loadVersion = 0;
let _unsubscribeDataChanges = null;

// ── 数据边界与筛选参数 ───────────────────────────────────────────────────────

function dateRangeForFilter(filter) {
    const range = derivePresetRange(filter, {
        today: todayRangeKey(),
        customStart: _customDateStart,
        customEnd: _customDateEnd,
        customFallback: '',
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
        income: Math.max(0, finiteNumber(raw.income)),
        expense: Math.max(0, finiteNumber(raw.expense)),
        transfer: Math.max(0, finiteNumber(raw.transfer)),
        balance: finiteNumber(raw.balance),
        count: nonNegativeInteger(raw.count),
    };
}

function currentFilterParams() {
    const params = { ...dateRangeForFilter(_dateFilter) };
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
    const res = await api.get('/items/aggregate', params);
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

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('ledger-page', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .ledger-hero {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 18px;
            align-items: center;
            padding: 24px 26px;
            border-radius: 28px;
            margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(239,68,68,0.16), transparent 34%),
                radial-gradient(circle at bottom left, rgba(251,146,60,0.12), transparent 28%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(255,247,245,0.95));
            border: 1px solid rgba(239,68,68,0.14);
            box-shadow: 0 18px 40px rgba(225,82,65,0.06);
        }
        .ledger-hero-copy h2 {
            margin: 0;
            font-size: 30px;
            font-weight: 820;
            letter-spacing: -0.03em;
            color: #b91c1c;
        }
        .ledger-hero-copy p {
            margin: 8px 0 0;
            font-size: 14px;
            line-height: 1.75;
            color: var(--color-text-secondary);
        }
        .ledger-hero-tags {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 14px;
        }
        .ledger-hero-tag {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            height: 34px;
            padding: 0 14px;
            border-radius: 999px;
            background: rgba(239,68,68,0.08);
            color: #b91c1c;
            font-size: 12px;
            font-weight: 700;
        }
        .ledger-page-header {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 10px;
        }
        .ledger-page-header-note {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(255,255,255,0.76);
            border: 1px solid rgba(239,68,68,0.12);
            color: #9f1239;
            font-size: 12px;
            font-weight: 700;
        }
        .ledger-section-stack {
            display: flex;
            flex-direction: column;
            gap: 18px;
        }

        /* 汇总卡片 */
        .ledger-summary-cards {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
        }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .ledger-summary-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .ledger-summary-cards { grid-template-columns: 1fr; }
        `,
        )}
        .ledger-summary-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(255,249,247,0.94));
            border: 1px solid rgba(239,68,68,0.12);
            border-radius: 22px;
            padding: 18px;
            display: grid;
            grid-template-columns: auto minmax(0, 1fr);
            align-items: center;
            gap: 14px;
            box-shadow: 0 14px 30px rgba(225,82,65,0.05);
            min-width: 0;
        }
        .ledger-summary-icon {
            width: 48px;
            height: 48px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 16px;
            font-size: 24px;
            background: rgba(239,68,68,0.08);
            flex-shrink: 0;
        }
        .ledger-summary-value {
            font-size: clamp(24px, 2.5vw, 31px);
            font-weight: 820;
            line-height: 1.04;
            letter-spacing: -0.03em;
            min-width: 0;
            overflow-wrap: anywhere;
        }
        .ledger-amount-income { color: var(--color-success); }
        .ledger-amount-expense { color: var(--color-ledger); }
        .ledger-amount-transfer { color: var(--color-text); }
        .ledger-summary-label {
            font-size: 12px;
            font-weight: 700;
            color: var(--color-text-secondary);
            margin-top: 6px;
        }
        .ledger-controls-grid {
            display: grid;
            grid-template-columns: minmax(360px, 1.05fr) minmax(320px, 0.95fr);
            gap: 16px;
            align-items: start;
        }
        .ledger-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(255,249,247,0.95));
            border: 1px solid rgba(239,68,68,0.12);
            border-radius: 24px;
            box-shadow: 0 16px 34px rgba(225,82,65,0.05);
            overflow: visible;
        }
        .ledger-panel-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            padding: 18px 20px 0;
        }
        .ledger-panel-head h3 {
            margin: 0;
            font-size: 18px;
            font-weight: 780;
            color: var(--color-text);
            letter-spacing: -0.02em;
        }
        .ledger-panel-head p {
            margin: 6px 0 0;
            font-size: 13px;
            color: var(--color-text-secondary);
        }
        .ledger-panel-body {
            padding: 16px 20px 20px;
        }

        .ledger-insights-panel {
            display: grid;
            grid-template-columns: minmax(0, 1.28fr) minmax(320px, 0.92fr);
            gap: 14px;
            margin-bottom: 18px;
        }
        .ledger-insights-main,
        .ledger-insights-side {
            display: flex;
            flex-direction: column;
            gap: 14px;
            min-width: 0;
        }
        .ledger-insight-card {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(255,248,246,0.98) 100%);
            border: 1px solid rgba(239,68,68,0.14);
            border-radius: 18px;
            box-shadow: 0 10px 30px rgba(225,82,65,0.06);
            padding: 16px 18px;
            min-width: 0;
            overflow: hidden;
        }
        .ledger-insight-card-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 14px;
        }
        .ledger-insight-card-head h3 {
            font-size: 15px;
            font-weight: 700;
            color: #7f1d1d;
            margin: 0;
        }
        .ledger-insight-card-head p {
            font-size: 12px;
            color: var(--color-text-secondary);
            margin: 4px 0 0;
        }
        .ledger-insight-badge {
            display: inline-flex;
            align-items: center;
            height: 24px;
            padding: 0 10px;
            border-radius: 999px;
            background: rgba(239,68,68,0.10);
            color: #b91c1c;
            font-size: 11px;
            font-weight: 700;
            flex-shrink: 0;
        }
        .ledger-pulse-metrics {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 14px;
        }
        .ledger-pulse-metric {
            padding: 10px 12px;
            border-radius: 14px;
            background: rgba(255,255,255,0.78);
            border: 1px solid rgba(239,68,68,0.08);
        }
        .ledger-pulse-label {
            display: block;
            font-size: 11px;
            color: var(--color-text-secondary);
            margin-bottom: 6px;
        }
        .ledger-pulse-metric strong {
            display: block;
            font-size: 18px;
            font-weight: 700;
            color: #111827;
            line-height: 1.2;
        }
        .ledger-pulse-metric small {
            display: block;
            margin-top: 4px;
            font-size: 11px;
            color: var(--color-text-secondary);
        }
        .ledger-pulse-metric .is-up { color: #dc2626; }
        .ledger-pulse-metric .is-down { color: #059669; }
        .ledger-insight-ring-wrap {
            display: grid;
            grid-template-columns: 188px minmax(0, 1fr);
            gap: 12px;
            align-items: center;
        }
        .ledger-insight-svg {
            display: block;
            width: 100%;
            height: auto;
        }
        .ledger-insight-svg-ring {
            max-width: 188px;
            margin: 0 auto;
        }
        .ledger-insight-axis-labels text {
            fill: #9ca3af;
            font-size: 10px;
            font-weight: 500;
        }
        .ledger-insight-y-label {
            fill: #94a3b8;
            font-size: 9px;
            font-weight: 600;
            letter-spacing: 0.01em;
        }
        .ledger-insight-svg-trend .ledger-insight-axis-labels text,
        .ledger-insight-svg-kline .ledger-insight-axis-labels text {
            font-size: 9px;
            font-weight: 500;
            letter-spacing: 0.01em;
        }
        .ledger-insight-svg-trend,
        .ledger-insight-svg-kline {
            transform: translateY(-2px);
        }
        .ledger-ring-center-value {
            fill: #7f1d1d;
            font-size: 17px;
            font-weight: 700;
        }
        .ledger-ring-center-label {
            fill: #9ca3af;
            font-size: 11px;
        }
        .ledger-insight-legend {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .ledger-insight-legend-item {
            display: grid;
            grid-template-columns: 10px minmax(0, 1fr) auto;
            align-items: center;
            gap: 8px;
            font-size: 12px;
        }
        .ledger-insight-legend-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }
        .ledger-insight-legend-name {
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: var(--color-text);
        }
        .ledger-insight-legend-value {
            color: #7f1d1d;
            font-weight: 700;
        }
        .ledger-hotspot-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .ledger-hotspot-row {
            padding: 10px 12px;
            border-radius: 14px;
            background: rgba(255,255,255,0.72);
            border: 1px solid rgba(239,68,68,0.08);
        }
        .ledger-hotspot-row-head {
            display: grid;
            grid-template-columns: 28px minmax(0, 1fr) auto;
            gap: 8px;
            align-items: center;
            margin-bottom: 8px;
        }
        .ledger-hotspot-rank {
            font-size: 11px;
            font-weight: 800;
            color: #dc2626;
            letter-spacing: 0.04em;
        }
        .ledger-hotspot-name {
            min-width: 0;
            font-size: 13px;
            font-weight: 600;
            color: var(--color-text);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .ledger-hotspot-amount {
            font-size: 13px;
            font-weight: 700;
            color: #7f1d1d;
        }
        .ledger-hotspot-track {
            position: relative;
            height: 10px;
            border-radius: 999px;
            background: rgba(239,68,68,0.08);
            overflow: hidden;
        }
        .ledger-hotspot-fill {
            height: 100%;
            border-radius: inherit;
            background: linear-gradient(90deg, #E15241 0%, #F59E0B 100%);
        }
        .ledger-hotspot-meta {
            margin-top: 6px;
            font-size: 11px;
            color: var(--color-text-secondary);
        }
        .ledger-insight-empty {
            min-height: 190px;
            display: flex;
            align-items: center;
            justify-content: center;
            text-align: center;
            color: var(--color-text-secondary);
            font-size: 13px;
            line-height: 1.7;
            padding: 12px;
            background: rgba(255,255,255,0.55);
            border-radius: 14px;
            border: 1px dashed rgba(239,68,68,0.16);
        }

        /* 快速记账 */
        .ledger-quick-add {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px 16px;
            align-items: end;
        }
        .ledger-quick-add > * { min-width: 0; }
        .ledger-qa-field {
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 7px;
        }
        .ledger-qa-label {
            font-size: 11px;
            font-weight: 800;
            color: var(--color-text-secondary);
            letter-spacing: 0.05em;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .ledger-quick-add input {
            box-sizing: border-box;
            font-size: 13px;
            height: 42px;
            border-radius: 14px;
            border: 1px solid rgba(239,68,68,0.14);
            width: 100%;
            padding: 0 14px;
            background: rgba(255,255,255,0.92);
        }
        .ledger-quick-add input:focus {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.1);
            outline: none;
        }
        .ledger-quick-add .pselect,
        .ledger-quick-add .pselect-trigger { width: 100%; }
        .ledger-qa-amount::-webkit-outer-spin-button,
        .ledger-qa-amount::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .ledger-qa-amount { -moz-appearance: textfield; }
        .ledger-qa-action {
            min-width: 0;
            align-self: end;
        }
        .ledger-qa-submit {
            width: 100%;
            height: 42px;
            padding: 0 16px;
            font-size: 13px;
            background: var(--color-ledger);
            color: #fff;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            font-weight: 700;
            transition: background .15s, transform .15s, box-shadow .15s;
            box-shadow: 0 10px 24px rgba(225,82,65,0.18);
        }
        .ledger-qa-submit:hover { background: #dc2626; transform: translateY(-1px); }
        .ledger-qa-submit:disabled { opacity: 0.58; cursor: wait; transform: none; }

        /* 筛选栏 */
        .ledger-filter-bar {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            align-items: flex-start;
            gap: 18px 20px;
        }
        .ledger-filter-item {
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 8px;
            min-width: 0;
            padding: 0;
            border: none;
            background: transparent;
            border-radius: 0;
        }
        .ledger-filter-item--amount {
            grid-column: 1 / -1;
            width: 100%;
        }
        .ledger-filter-item--date.is-custom {
            grid-column: 1 / -1;
        }
        .ledger-filter-item--date .ledger-filter-controls,
        .ledger-filter-item--transaction-type .ledger-filter-controls,
        .ledger-filter-item--category .ledger-filter-controls,
        .ledger-filter-item--account .ledger-filter-controls { width: 100%; }
        .ledger-filter-item--amount .ledger-filter-controls {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
            align-items: center;
            gap: 8px;
            width: 100%;
        }
        .ledger-filter-bar label {
            font-size: 11px;
            font-weight: 800;
            color: var(--color-text-secondary);
            white-space: nowrap;
            flex-shrink: 0;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }
        .ledger-filter-controls {
            display: flex;
            gap: 8px;
            align-items: center;
            min-width: 0;
            flex-wrap: wrap;
            width: 100%;
        }
        .ledger-filter-range {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;
            align-items: center;
            gap: 8px;
            min-width: 0;
            flex: 1 0 100%;
            width: 100%;
            margin-top: 8px;
        }
        .ledger-filter-range[hidden] { display: none; }
        .ledger-filter-range-sep {
            font-size: 12px;
            color: var(--color-text-secondary);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
        }
        .ledger-amount-separator { font-size: 12px; color: var(--color-text-secondary); }
        .ledger-filter-date,
        .ledger-filter-transaction-type,
        .ledger-filter-account,
        .ledger-filter-category {
            width: 100%;
            flex: 0 0 auto;
        }
        .ledger-filter-item--date .pselect,
        .ledger-filter-item--transaction-type .pselect,
        .ledger-filter-item--account .pselect,
        .ledger-filter-item--category .pselect,
        .ledger-filter-item--date .pselect-trigger,
        .ledger-filter-item--transaction-type .pselect-trigger,
        .ledger-filter-item--account .pselect-trigger,
        .ledger-filter-item--category .pselect-trigger { width: 100%; }
        .ledger-amount-input,
        .ledger-custom-date-input {
            box-sizing: border-box;
            height: 40px !important;
            font-size: 13px;
            padding: 0 12px;
            border: 1px solid rgba(239,68,68,0.16);
            border-radius: 14px;
            background: rgba(255,255,255,0.94);
            color: var(--color-text);
            font-weight: 600;
            outline: none;
            transition: border-color .15s, background .15s, box-shadow .15s;
        }
        .ledger-filter-bar .ledger-amount-input {
            width: 100%;
            min-width: 0;
            flex: 1 1 auto;
            padding: 0 12px 0 14px;
            border-color: var(--color-border);
            border-radius: 14px;
            background: rgba(255,255,255,0.94);
            font-weight: 500;
        }
        .ledger-amount-input::-webkit-outer-spin-button,
        .ledger-amount-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .ledger-amount-input { -moz-appearance: textfield; }
        .ledger-filter-bar .ledger-custom-date-input {
            width: 100%;
            min-width: 105px;
            max-width: 100%;
            flex: 1 1 auto;
            font-weight: 400;
            border-color: var(--color-border);
        }
        .ledger-range-apply {
            height: 40px;
            padding: 0 14px;
            min-width: 64px;
            white-space: nowrap;
            border: 1px solid rgba(239,68,68,0.2);
            border-radius: 14px;
            background: rgba(239,68,68,0.06);
            color: #b91c1c;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: background .15s, border-color .15s, transform .15s;
            justify-self: start;
        }
        .ledger-range-apply:hover {
            background: rgba(239,68,68,0.12);
            border-color: rgba(239,68,68,0.28);
        }
        .ledger-range-apply:active { transform: translateY(1px); }
        .ledger-amount-input:hover,
        .ledger-custom-date-input:hover {
            border-color: var(--color-ledger);
            background: rgba(239,68,68,0.06);
        }
        .ledger-amount-input:focus,
        .ledger-custom-date-input:focus {
            border-color: var(--color-ledger);
            background: rgba(239,68,68,0.06);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }
        .ledger-amount-input::placeholder { color: var(--color-text-secondary); }
        .ledger-filter-bar .ledger-amount-input:hover,
        .ledger-filter-bar .ledger-custom-date-input:hover {
            border-color: #9CA3AF;
            background: #EEF0F2;
        }
        .ledger-filter-bar .ledger-amount-input:focus,
        .ledger-filter-bar .ledger-custom-date-input:focus {
            border-color: var(--color-ledger);
            background: rgba(255,255,255,0.94);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }

        /* 共享下拉框 */
        .ledger-filter-bar .pselect-trigger {
            height: 40px;
            padding: 0 12px 0 14px;
            border-radius: 14px;
            background: rgba(255,255,255,0.94);
        }
        .ledger-filter-bar .pselect-label { min-width: 0; }
        .ledger-filter-bar .pselect-panel { border-radius: 16px; z-index: 1200; }
        .ledger-quick-add .pselect-trigger {
            height: 42px;
            padding: 0 10px 0 12px;
            border-radius: 14px;
            font-weight: 600;
            background: rgba(255,255,255,0.94);
        }
        .ledger-quick-add .pselect-label { min-width: 0; }
        .ledger-quick-add .pselect-panel { border-radius: 16px; z-index: 1200; }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .ledger-controls-grid {
                grid-template-columns: 1fr;
            }
            .ledger-insights-panel {
                grid-template-columns: 1fr;
            }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .ledger-hero {
                grid-template-columns: 1fr;
                padding: 22px 20px;
            }
            .ledger-quick-add {
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 12px;
            }
            .ledger-qa-field--title,
            .ledger-qa-field--date,
            .ledger-qa-action {
                grid-column: 1 / -1;
            }
            .ledger-page-header {
                align-items: flex-start;
            }
            .ledger-filter-bar {
                grid-template-columns: 1fr;
            }
            .ledger-pulse-metrics {
                grid-template-columns: 1fr;
            }
            .ledger-insight-ring-wrap {
                grid-template-columns: 1fr;
            }
            .ledger-insight-svg-ring {
                max-width: min(200px, 52vw);
            }
            .ledger-ring-center-value {
                font-size: 15px;
            }
            .ledger-ring-center-label {
                font-size: 10px;
            }
            .ledger-insight-y-label {
                font-size: clamp(14px, 4.2vw, 18px);
            }
            .ledger-insight-svg-trend .ledger-insight-axis-labels text,
            .ledger-insight-svg-kline .ledger-insight-axis-labels text {
                font-size: clamp(13px, 3.8vw, 17px);
                font-weight: 600;
            }
            .ledger-filter-item {
                width: 100%;
                flex-wrap: wrap;
                align-items: flex-start;
            }
            .ledger-filter-item--date,
            .ledger-filter-item--transaction-type,
            .ledger-filter-item--account,
            .ledger-filter-item--category,
            .ledger-filter-item--amount {
                flex-basis: 100%;
            }
            .ledger-filter-controls,
            .ledger-filter-range {
                width: 100%;
                flex-wrap: wrap;
            }
            .ledger-filter-item--amount .ledger-filter-controls {
                grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
                align-items: center;
            }
            .ledger-filter-date,
            .ledger-filter-transaction-type,
            .ledger-filter-category {
                width: 100%;
            }
            .ledger-filter-range {
                align-items: stretch;
                grid-template-columns: 1fr;
                row-gap: 8px;
            }
            .ledger-filter-range-sep {
                display: none;
            }
            .ledger-filter-bar .ledger-custom-date-input,
            .ledger-filter-bar .ledger-amount-input {
                width: 100%;
                min-width: 105px;
                flex: 1 1 100%;
            }
            .ledger-range-apply {
                width: 100%;
                justify-self: stretch;
            }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.PHONE,
            `
            .ledger-quick-add {
                grid-template-columns: 1fr;
            }
            .ledger-qa-field--title,
            .ledger-qa-field--date,
            .ledger-qa-action {
                grid-column: auto;
            }
            .ledger-summary-value {
                font-size: 26px;
            }
        `,
        )}

        /* 按日期分组的账目列表 */
        .ledger-date-group { margin-bottom: 12px; }
        .ledger-date-group-header {
            font-size: 13px;
            font-weight: 600;
            color: var(--color-text-secondary);
            padding: 6px 0 4px;
            border-bottom: 1px solid var(--color-border);
        }
        .ledger-row {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 9px 4px;
            border-bottom: 1px solid var(--color-border);
        }
        .ledger-list-toolbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 14px;
            flex-wrap: wrap;
        }
        .ledger-list-toolbar-copy {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        .ledger-list-toolbar-copy strong {
            font-size: 14px;
            color: var(--color-text);
        }
        .ledger-list-toolbar-copy span {
            font-size: 12px;
            color: var(--color-text-secondary);
        }
        .ledger-sort-toggle {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px;
            border-radius: 999px;
            background: rgba(239,68,68,0.06);
            border: 1px solid rgba(239,68,68,0.12);
        }
        .ledger-sort-btn {
            height: 32px;
            padding: 0 12px;
            border: none;
            border-radius: 999px;
            background: transparent;
            color: var(--color-text-secondary);
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: background .15s, color .15s, box-shadow .15s;
        }
        .ledger-sort-btn:hover {
            color: var(--color-ledger);
        }
        .ledger-sort-btn.is-active {
            background: #fff;
            color: var(--color-ledger);
            box-shadow: 0 4px 14px rgba(239,68,68,0.12);
        }
        .ledger-row:last-child { border-bottom: none; }
        .ledger-row:hover { background: var(--color-hover, rgba(0,0,0,0.04)); }
        .ledger-dir-icon {
            font-size: 16px;
            flex-shrink: 0;
            width: 22px;
            text-align: center;
        }
        .ledger-row-main {
            flex: 1;
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 2px;
            padding: 0;
            border: 0;
            background: transparent;
            text-align: left;
            font: inherit;
            cursor: pointer;
        }
        .ledger-row-main:disabled { cursor: default; }
        .ledger-row-title {
            font-size: 14px;
            font-weight: 500;
            color: var(--color-text);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .ledger-row-meta {
            font-size: 11px;
            color: var(--color-text-secondary);
            white-space: nowrap;
        }
        .ledger-row-amount {
            font-size: 15px;
            font-weight: 700;
            flex-shrink: 0;
            min-width: 80px;
            text-align: right;
        }
        .ledger-row-actions {
            display: flex;
            gap: 4px;
            flex-shrink: 0;
        }
        .ledger-category-badge { font-size: 11px; }
        .ledger-flat-list .ledger-row:last-child { border-bottom: none; }
        .ledger-empty {
            text-align: center;
            padding: 40px 0;
            color: var(--color-text-tertiary);
            font-size: 14px;
        }
        .ledger-records-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(255,249,247,0.95));
            border: 1px solid rgba(239,68,68,0.12);
            border-radius: 26px;
            box-shadow: 0 18px 34px rgba(225,82,65,0.05);
            padding: 18px 20px 20px;
        }
        .ledger-pagination { margin-top: 16px; }
        .ledger-detail-shell { margin: -2px 0; }
        .ledger-detail-head {
            display: flex; align-items: baseline; gap: 8px; padding-bottom: 16px;
            border-bottom: 1px solid var(--color-border); margin-bottom: 14px;
        }
        .ledger-detail-amount { font-size: 28px; font-weight: 700; }
        .ledger-detail-type {
            font-size: 13px; padding: 2px 8px; border-radius: 20px; font-weight: 600;
        }
        .ledger-detail-type.ledger-amount-transfer { background: rgba(15,23,42,0.08); }
        .ledger-detail-type.ledger-amount-income { background: rgba(16,185,129,0.1); }
        .ledger-detail-type.ledger-amount-expense { background: rgba(239,68,68,0.1); }
        .ledger-detail-rows {
            padding: 0 14px; background: var(--color-bg); border: 1px solid var(--color-border);
            border-radius: 12px;
        }
        .ledger-detail-row { display: flex; gap: 12px; padding: 12px 0; }
        .ledger-detail-row:not(:last-child) { border-bottom: 1px solid var(--color-border); }
        .ledger-detail-label {
            width: 56px; flex-shrink: 0; font-size: 12px; color: var(--color-text-secondary);
            padding-top: 2px;
        }
        .ledger-detail-value {
            flex: 1; font-size: 14px; color: var(--color-text); word-break: break-word;
            line-height: 1.6; white-space: pre-wrap;
        }
        .ledger-modal-delete { margin-right: auto; }
        .ledger-qa-submit:focus-visible,
        .ledger-range-apply:focus-visible,
        .ledger-sort-btn:focus-visible,
        .ledger-row-main:focus-visible,
        .ledger-row-actions button:focus-visible {
            outline: 3px solid rgba(239,68,68,0.28); outline-offset: 2px;
        }
    `,
    );
}

// ── render ────────────────────────────────────────────────────────────────────

function renderSummaryCards() {
    const { income, expense, transfer = 0, balance } = _summaryData;
    const balanceClass = balance >= 0 ? 'ledger-amount-income' : 'ledger-amount-expense';
    return `
        <div class="ledger-summary-cards">
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">📈</div>
                <div>
                    <div class="ledger-summary-value ledger-amount-income">${formatAmount(income)}</div>
                    <div class="ledger-summary-label">收入</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">📉</div>
                <div>
                    <div class="ledger-summary-value ledger-amount-expense">${formatAmount(expense)}</div>
                    <div class="ledger-summary-label">支出</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">💰</div>
                <div>
                    <div class="ledger-summary-value ${balanceClass}">${formatAmount(balance)}</div>
                    <div class="ledger-summary-label">结余</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">🔁</div>
                <div>
                    <div class="ledger-summary-value ledger-amount-transfer">${formatAmount(transfer)}</div>
                    <div class="ledger-summary-label">转账</div>
                </div>
            </div>
        </div>`;
}

function renderQuickAdd() {
    const today = todayRangeKey();
    const typeSelect = renderCustomSelect({
        id: 'qa-transaction-type',
        options: TRANSACTION_TYPE_OPTIONS,
        selected: 'expense',
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
                <label class="ledger-qa-label" for="qa-amount">金额</label>
                <input type="number" class="ledger-qa-amount" id="qa-amount" placeholder="0.00" step="0.01" min="0" aria-label="金额">
            </div>
            <div class="ledger-qa-field ledger-qa-field--title">
                <label class="ledger-qa-label" for="qa-title">摘要</label>
                <input type="text" class="ledger-qa-title" id="qa-title" placeholder="餐饮、交通、工资..." aria-label="摘要">
            </div>
            <div class="ledger-qa-field ledger-qa-field--category">
                <label class="ledger-qa-label" for="qa-category">分类</label>
                <input type="text" class="ledger-qa-category" id="qa-category" placeholder="其他" aria-label="分类">
            </div>
            <div class="ledger-qa-field ledger-qa-field--account">
                <label class="ledger-qa-label" for="qa-account">账户</label>
                <input type="text" class="ledger-qa-account" id="qa-account" placeholder="现金" aria-label="账户">
            </div>
            <div class="ledger-qa-field ledger-qa-field--counter">
                <label class="ledger-qa-label" for="qa-counter">转入账户</label>
                <input type="text" class="ledger-qa-counter" id="qa-counter" placeholder="仅转账填写" aria-label="转入账户">
            </div>
            <div class="ledger-qa-field ledger-qa-field--merchant">
                <label class="ledger-qa-label" for="qa-merchant">商户/对方</label>
                <input type="text" class="ledger-qa-merchant" id="qa-merchant" placeholder="可选" aria-label="商户或对方">
            </div>
            <div class="ledger-qa-field ledger-qa-field--date">
                <label class="ledger-qa-label" for="qa-date">日期</label>
                <input type="text" class="ledger-qa-date" id="qa-date" value="${escapeHtml(today)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="记账日期">
            </div>
            <div class="ledger-qa-action">
                <button type="button" class="ledger-qa-submit" id="qa-submit">+ 记录</button>
            </div>
        </div>`;
}

function renderFilterBar() {
    const typeOptions = [{ value: '', label: '全部类型' }, ...TRANSACTION_TYPE_OPTIONS];
    const catOptions = [
        { value: '', label: '全部分类' },
        ..._allCategories.map((category) => ({ value: category, label: category })),
    ];
    const accountOptions = [
        { value: '', label: '全部账户' },
        ..._allAccounts.map((account) => ({ value: account, label: account })),
    ];

    return `
        <div class="ledger-filter-bar" id="ledger-filter-bar">
            <div class="ledger-filter-item ledger-filter-item--date${_dateFilter === 'custom' ? ' is-custom' : ''}">
                <label id="filter-date-label">时段：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-date', options: DATE_FILTER_OPTIONS, selected: _dateFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-date', labelledBy: 'filter-date-label' })}
                    <div class="ledger-filter-range" id="filter-custom-range" ${_dateFilter === 'custom' ? '' : 'hidden'}>
                        <input type="text" class="ledger-custom-date-input" id="filter-date-start" value="${escapeHtml(_customDateStart)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="筛选开始日期">
                        <span class="ledger-filter-range-sep">至</span>
                        <input type="text" class="ledger-custom-date-input" id="filter-date-end" value="${escapeHtml(_customDateEnd)}" inputmode="numeric" placeholder="YYYY-MM-DD" aria-label="筛选结束日期">
                        <button type="button" class="ledger-range-apply" id="filter-date-apply">应用</button>
                    </div>
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--transaction-type">
                <label id="filter-transaction-type-label">类型：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-transaction-type', options: typeOptions, selected: _transactionTypeFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-transaction-type', labelledBy: 'filter-transaction-type-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--account">
                <label id="filter-account-label">账户：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-account', options: accountOptions, selected: _accountFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-account', labelledBy: 'filter-account-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--category">
                <label id="filter-category-label">分类：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-category', options: catOptions, selected: _categoryFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-category', labelledBy: 'filter-category-label' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--amount">
                <label>金额：</label>
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
    const txType = normalizedTransactionType(item.transaction_type);
    const isIncome = txType === 'income';
    const isTransfer = txType === 'transfer';
    const dirIcon = isTransfer ? '↔' : isIncome ? '⬆️' : '⬇️';
    const amountClass = isTransfer
        ? 'ledger-amount-transfer'
        : isIncome
          ? 'ledger-amount-income'
          : 'ledger-amount-expense';
    const amountSign = isTransfer ? '↔ ' : isIncome ? '+' : '-';
    const meta =
        _sortMode === 'amount' && item.ledger_date
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
            <span class="ledger-row-amount ${amountClass}">${amountSign}${formatAmount(item.amount)}</span>
            <div class="ledger-row-actions">
                <button type="button" class="btn btn-icon btn-sm btn-edit-ledger" data-id="${escapedId}" aria-label="编辑：${escapeHtml(title)}" ${itemId ? '' : 'disabled'}>✏️</button>
                <button type="button" class="btn btn-icon btn-sm btn-delete-ledger" data-id="${escapedId}" aria-label="删除：${escapeHtml(title)}" ${itemId ? '' : 'disabled'}>🗑️</button>
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
    ensureStyles();

    _container.innerHTML = `
        <div class="ledger-page">
            <section class="ledger-hero">
                <div class="ledger-hero-copy">
                    <h2>💰 账本</h2>
                    <p>记录每一笔收支，并查看当前范围内的资金变化。</p>
                    <div class="ledger-hero-tags">
                        <span class="ledger-hero-tag">${DATE_FILTER_LABELS[_dateFilter] || '当前范围'}</span>
                        <span class="ledger-hero-tag">${_total} 条账目</span>
                        <span class="ledger-hero-tag">${_sortMode === 'amount' ? '按金额排序' : '按时间排序'}</span>
                    </div>
                </div>
                <div class="ledger-page-header">
                    <span class="ledger-page-header-note">查看当前范围收支分析</span>
                </div>
            </section>
            <div class="ledger-section-stack">
                ${renderSummaryCards()}
                <section class="ledger-controls-grid">
                    <div class="ledger-panel">
                        <div class="ledger-panel-head">
                            <div>
                                <h3>快速记一笔</h3>
                                <p>快速补记一笔账，同时保留分类和日期信息。</p>
                            </div>
                        </div>
                        <div class="ledger-panel-body">
                            ${renderQuickAdd()}
                        </div>
                    </div>
                    <div class="ledger-panel">
                        <div class="ledger-panel-head">
                            <div>
                                <h3>筛选范围</h3>
                                <p>按时间、方向、分类和金额筛选账目。</p>
                            </div>
                        </div>
                        <div class="ledger-panel-body">
                            ${renderFilterBar()}
                        </div>
                    </div>
                </section>
                ${renderLedgerInsightsPanel(_insightsData)}
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
    const root = _container;

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

    const dateStart = root.querySelector('#filter-date-start');
    const dateEnd = root.querySelector('#filter-date-end');
    const dateApply = root.querySelector('#filter-date-apply');
    const applyCustomDateRange = async () => {
        const nextStart = dateStart?.value.trim() || '';
        const nextEnd = dateEnd?.value.trim() || '';
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
                const nextMin = root.querySelector('#filter-amount-min')?.value.trim() || '';
                const nextMax = root.querySelector('#filter-amount-max')?.value.trim() || '';
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
            const editBtn = event.target.closest('.btn-edit-ledger');
            const deleteBtn = event.target.closest('.btn-delete-ledger');
            const openBtn = event.target.closest('[data-open-ledger]');
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
    const root = _container;
    const typeSelect = root.querySelector('#qa-transaction-type');
    const amountInput = root.querySelector('#qa-amount');
    const titleInput = root.querySelector('#qa-title');
    const categoryInput = root.querySelector('#qa-category');
    const accountInput = root.querySelector('#qa-account');
    const counterInput = root.querySelector('#qa-counter');
    const merchantInput = root.querySelector('#qa-merchant');
    const dateInput = root.querySelector('#qa-date');
    const submitButton = root.querySelector('#qa-submit');
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
            currency: 'CNY',
        });
        showToast('记录已添加', 'success');
        amountInput.value = '';
        titleInput.value = '';
        merchantInput.value = '';
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
    const txType = normalizedItem.transaction_type;
    const isIncome = txType === 'income';
    const isTransfer = txType === 'transfer';
    const amountClass = isTransfer
        ? 'ledger-amount-transfer'
        : isIncome
          ? 'ledger-amount-income'
          : 'ledger-amount-expense';
    const amountSign = isTransfer ? '↔ ' : isIncome ? '+' : '-';
    const directionLabel = isTransfer ? '转账' : isIncome ? '收入' : '支出';
    const accountText =
        isTransfer && normalizedItem.counter_account_name
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
                <span class="ledger-detail-amount ${amountClass}">${amountSign}${formatAmount(normalizedItem.amount)}</span>
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
        closeModal();
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
    const footer = `
        <button type="button" class="btn btn-danger btn-sm ledger-modal-delete" id="modal-delete">删除</button>
        <button type="button" class="btn btn-secondary" id="modal-cancel">取消</button>
        <button type="button" class="btn btn-primary" id="modal-save">保存</button>`;

    const content = showModal('编辑记录', safeHtml(bodyHTML), { footer: safeHtml(footer) });
    initFormInteractions(content);

    const form = content.querySelector('#ledger-edit-form');
    const saveButton = content.querySelector('#modal-save');
    bindFormSubmit(form, saveButton);
    content.querySelector('#modal-cancel').onclick = closeModal;
    content.querySelector('#modal-delete').onclick = async () => {
        closeModal();
        await handleDelete(normalizedItem.id);
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
            await api.put(`/items/${encodeURIComponent(normalizedItem.id)}`, data);
            showToast('记录已更新', 'success');
            closeModal();
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

async function handleDelete(id) {
    const normalizedId = String(id ?? '');
    if (!normalizedId) {
        showToast('无法删除缺少编号的账目记录', 'error');
        return;
    }
    const item = _items.find((entry) => entry.id === normalizedId);
    const label = item?.title?.trim() || '这条记录';
    const confirmed = await showConfirmModal({
        title: '删除记录',
        message: `确定要删除“${label}”吗？删除后这条账目会从当前列表和统计中移除。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) return;
    try {
        await api.delete(`/items/${encodeURIComponent(normalizedId)}`);
        showToast('记录已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'ledger' } }));
    } catch (err) {
        showToast(`删除失败：${err?.message || '未知错误'}`, 'error');
    }
}

// ── 数据加载与生命周期 ───────────────────────────────────────────────────────

async function loadAndRender(refreshCategories = false) {
    if (!_container) return;
    const container = _container;
    const requestVersion = ++_loadVersion;
    try {
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
    _sortMode = 'date';
    _summaryData = { income: 0, expense: 0, transfer: 0, balance: 0, count: 0 };
    _insightsData = null;
    _allCategories = [];
    _allAccounts = ['现金'];
    _quickAddSaving = false;

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
