import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';
import { renderLedgerInsightsPanel } from '../components/ledger_insights.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { formatAmount, isoDate, isValidDateInput, todayStr as sharedTodayStr } from '../utils/format.js';
import { derivePresetRange, todayRangeKey } from '../utils/date_ranges.js';
import { BREAKPOINTS, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

// ── constants ─────────────────────────────────────────────────────────────────

const PAGE_SIZE = 50;

const LEDGER_FIELDS = [
    { name: 'direction', label: '类型', type: 'select', options: [
        { value: 'expense', label: '支出' },
        { value: 'income',  label: '收入' },
    ]},
    { name: 'amount',          label: '金额', type: 'number', required: true, step: '0.01' },
    { name: 'title',           label: '摘要', type: 'text',   required: true },
    { name: 'ledger_category', label: '分类', type: 'text',   placeholder: '其他' },
    { name: 'ledger_date',     label: '日期', type: 'date' },
    { name: 'remark',          label: '备注', type: 'textarea' },
];

const CSS_ID = 'pendo-ledger-styles';

// ── module state ──────────────────────────────────────────────────────────────

let _container          = null;
let _items              = [];
let _total              = 0;
let _page               = 1;
let _dateFilter         = 'month';   // 'today' | 'week' | 'month' | 'quarter' | 'year' | 'last_year' | 'all' | 'custom'
let _directionFilter    = '';        // '' | 'income' | 'expense'
let _categoryFilter     = '';
let _amountMin          = '';
let _amountMax          = '';
let _sortMode           = 'date';    // 'date' | 'amount'
let _customDateStart    = '';
let _customDateEnd      = '';
let _summaryData        = { income: 0, expense: 0, balance: 0, count: 0 };
let _insightsData       = null;
let _allCategories      = [];
let _dataChangedHandler = null;

// ── date helpers ──────────────────────────────────────────────────────────────

function todayStr() {
    return todayRangeKey() || sharedTodayStr();
}

function dateRangeForFilter(filter) {
    if (filter === 'today') {
        const value = todayStr();
        return { start_date: value, end_date: value };
    }
    const range = derivePresetRange(filter, {
        today: todayStr(),
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

// ── helpers ───────────────────────────────────────────────────────────────────

function groupByDate(items) {
    const groups = {};
    items.forEach(item => {
        const d = item.ledger_date || '未知日期';
        if (!groups[d]) groups[d] = [];
        groups[d].push(item);
    });
    const sorted = Object.keys(groups).sort((a, b) => b.localeCompare(a));
    return sorted.map(date => ({ date, items: groups[date] }));
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchItems(page) {
    const params = {
        type:       'ledger',
        date_field: 'ledger_date',
        sort:       _sortMode === 'amount' ? 'amount' : 'ledger_date',
        order:      'desc',
        page,
        page_size:  PAGE_SIZE,
    };
    const range = dateRangeForFilter(_dateFilter);
    if (range.start_date) params.start_date = range.start_date;
    if (range.end_date)   params.end_date   = range.end_date;
    if (_directionFilter) params.direction  = _directionFilter;
    if (_categoryFilter)  params.category   = _categoryFilter;
    if (_amountMin !== '') params.amount_min = parseFloat(_amountMin);
    if (_amountMax !== '') params.amount_max = parseFloat(_amountMax);

    const res = await api.get('/items', params);
    return {
        items: res.data?.items ?? [],
        total: res.data?.total ?? 0,
    };
}

async function fetchAggregate() {
    const params = { type: 'ledger', date_field: 'ledger_date' };
    const range = dateRangeForFilter(_dateFilter);
    if (range.start_date) params.start_date = range.start_date;
    if (range.end_date)   params.end_date   = range.end_date;
    if (_directionFilter) params.direction  = _directionFilter;
    if (_categoryFilter)  params.category   = _categoryFilter;
    if (_amountMin !== '') params.amount_min = parseFloat(_amountMin);
    if (_amountMax !== '') params.amount_max = parseFloat(_amountMax);

    const res = await api.get('/items/aggregate', params);
    return res.data ?? { income: 0, expense: 0, balance: 0, count: 0 };
}

async function fetchCategories() {
    try {
        const res = await api.get('/items/categories', { type: 'ledger' });
        return res.data?.categories ?? [];
    } catch {
        return [];
    }
}

async function fetchInsights() {
    const params = {};
    if (_dateFilter !== 'all') {
        const range = dateRangeForFilter(_dateFilter);
        if (range.start_date) params.start_date = range.start_date;
        if (range.end_date) params.end_date = range.end_date;
    }
    params.compare_mode = compareModeForFilter(_dateFilter);
    if (_directionFilter) params.direction = _directionFilter;
    if (_categoryFilter) params.category = _categoryFilter;
    if (_amountMin !== '') params.amount_min = parseFloat(_amountMin);
    if (_amountMax !== '') params.amount_max = parseFloat(_amountMax);

    const res = await api.get('/stats/ledger/insights', params);
    return res.data ?? null;
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    injectStyles(CSS_ID, `
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

        /* Summary cards */
        .ledger-summary-cards {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
        }
        ${mediaMax(BREAKPOINTS.XL, `
            .ledger-summary-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
            .ledger-summary-cards { grid-template-columns: 1fr; }
        `)}
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

        /* Quick-add bar */
        .ledger-quick-add {
            --ledger-qa-direction-width: 128px;
            --ledger-qa-control-width: 176px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
        }
        .ledger-quick-add > * { min-width: 0; }
        .ledger-quick-add input {
            box-sizing: border-box;
            font-size: 13px;
            height: 40px;
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
        .ledger-quick-add .pselect { width: auto; max-width: 100%; }
        .ledger-quick-add .ledger-qa-direction {
            flex: 0 0 auto;
            width: var(--ledger-qa-direction-width);
            max-width: 100%;
        }
        .ledger-quick-add .ledger-qa-amount,
        .ledger-quick-add .ledger-qa-title,
        .ledger-quick-add .ledger-qa-category,
        .ledger-quick-add .ledger-qa-date,
        .ledger-quick-add .ledger-qa-submit {
            flex: 0 0 auto;
            width: min(100%, var(--ledger-qa-control-width));
            min-width: 0;
        }
        .ledger-qa-amount::-webkit-outer-spin-button,
        .ledger-qa-amount::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .ledger-qa-amount { -moz-appearance: textfield; }
        .ledger-qa-submit {
            height: 40px;
            padding: 0 16px;
            font-size: 13px;
            flex-shrink: 0;
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

        /* Filter bar */
        .ledger-filter-bar {
            --ledger-filter-select-width: 170px;
            --ledger-filter-control-width: 196px;
            --ledger-filter-amount-width: 312px;
            display: flex;
            flex-wrap: wrap;
            align-items: flex-start;
            gap: 14px;
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
        .ledger-filter-item--date {
            flex: 0 0 auto;
            width: min(100%, var(--ledger-filter-control-width));
        }
        .ledger-filter-item--direction {
            flex: 0 0 auto;
            width: min(100%, var(--ledger-filter-select-width));
        }
        .ledger-filter-item--category {
            flex: 0 0 auto;
            width: min(100%, var(--ledger-filter-select-width));
        }
        .ledger-filter-item--amount {
            flex: 0 0 auto;
            width: min(100%, var(--ledger-filter-amount-width));
        }
        .ledger-filter-item--date .ledger-filter-controls,
        .ledger-filter-item--direction .ledger-filter-controls,
        .ledger-filter-item--category .ledger-filter-controls { width: 100%; }
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
        }
        .ledger-filter-range {
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            align-items: stretch;
            row-gap: 8px;
            min-width: 0;
            flex: 1 0 100%;
            width: 100%;
            margin-top: 4px;
        }
        .ledger-filter-range-sep {
            font-size: 12px;
            color: var(--color-text-secondary);
            display: none;
            flex: 0 0 auto;
        }
        .ledger-filter-date,
        .ledger-filter-direction,
        .ledger-filter-category {
            width: 100%;
            flex: 0 0 auto;
        }
        .ledger-filter-item--date .pselect,
        .ledger-filter-item--direction .pselect,
        .ledger-filter-item--category .pselect,
        .ledger-filter-item--date .pselect-trigger,
        .ledger-filter-item--direction .pselect-trigger,
        .ledger-filter-item--category .pselect-trigger { width: 100%; }
        .ledger-amount-input,
        .ledger-custom-date-input {
            box-sizing: border-box;
            height: 36px !important;
            font-size: 13px;
            padding: 0 12px;
            border: 1px solid rgba(239,68,68,0.35);
            border-radius: 20px;
            background: var(--color-bg);
            color: #b91c1c;
            font-weight: 500;
            outline: none;
            transition: border-color .15s, background .15s, box-shadow .15s;
        }
        .ledger-filter-bar .ledger-amount-input {
            width: 100%;
            min-width: 0;
            flex: 1 1 auto;
        }
        .ledger-amount-input::-webkit-outer-spin-button,
        .ledger-amount-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .ledger-amount-input { -moz-appearance: textfield; }
        .ledger-filter-bar .ledger-custom-date-input {
            width: 100%;
            min-width: 0;
            max-width: 100%;
            flex: 1 1 auto;
        }
        .ledger-range-apply {
            height: 36px;
            padding: 0 14px;
            min-width: 64px;
            white-space: nowrap;
            border: 1px solid rgba(239,68,68,0.2);
            border-radius: 18px;
            background: rgba(239,68,68,0.06);
            color: #b91c1c;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: background .15s, border-color .15s, transform .15s;
            grid-column: 1 / -1;
            justify-self: stretch;
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

        /* Shared custom select */
        .ledger-filter-bar .pselect-trigger {
            height: 40px;
            padding: 0 12px 0 14px;
            border-radius: 14px;
            background: rgba(255,255,255,0.94);
        }
        .ledger-filter-bar .pselect-label { min-width: 0; }
        .ledger-filter-bar .pselect-panel { border-radius: 16px; z-index: 1200; }
        .ledger-quick-add .pselect-trigger {
            height: 40px;
            padding: 0 10px 0 12px;
            border-radius: 14px;
            font-weight: 600;
            background: rgba(255,255,255,0.94);
        }
        .ledger-quick-add .pselect-label { min-width: 0; }
        .ledger-quick-add .pselect-panel { border-radius: 16px; z-index: 1200; }
        ${mediaMax(BREAKPOINTS.XL, `
            .ledger-controls-grid {
                grid-template-columns: 1fr;
            }
            .ledger-insights-panel {
                grid-template-columns: 1fr;
            }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
            .ledger-hero {
                grid-template-columns: 1fr;
                padding: 22px 20px;
            }
            .ledger-quick-add {
                --ledger-qa-direction-width: 108px;
                --ledger-qa-control-width: 136px;
            }
            .ledger-page-header {
                align-items: flex-start;
            }
            .ledger-filter-bar {
                --ledger-filter-select-width: 100%;
                --ledger-filter-control-width: 100%;
                --ledger-filter-amount-width: 100%;
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
            .ledger-filter-item--direction,
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
            .ledger-filter-direction,
            .ledger-filter-category {
                width: 100%;
            }
            .ledger-filter-range {
                flex-direction: column;
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
                min-width: 0;
                flex: 1 1 100%;
            }
            .ledger-range-apply {
                width: 100%;
                justify-self: stretch;
            }
        `)}
        ${mediaMax(BREAKPOINTS.PHONE, `
            .ledger-summary-value {
                font-size: 26px;
            }
        `)}

        /* Date group list */
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
        .ledger-row { cursor: pointer; }
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
        }
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
    `);
}

// ── render ────────────────────────────────────────────────────────────────────

function renderSummaryCards() {
    const { income, expense, balance } = _summaryData;
    const balanceColor = balance >= 0 ? 'var(--color-success)' : 'var(--color-ledger)';
    return `
        <div class="ledger-summary-cards">
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">📈</div>
                <div>
                    <div class="ledger-summary-value" style="color:var(--color-success);">${formatAmount(income)}</div>
                    <div class="ledger-summary-label">收入</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">📉</div>
                <div>
                    <div class="ledger-summary-value" style="color:var(--color-ledger);">${formatAmount(expense)}</div>
                    <div class="ledger-summary-label">支出</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">💰</div>
                <div>
                    <div class="ledger-summary-value" style="color:${balanceColor};">${formatAmount(balance)}</div>
                    <div class="ledger-summary-label">结余</div>
                </div>
            </div>
        </div>`;
}

function renderQuickAdd() {
    const today = todayStr();
    const dirSelect = renderCustomSelect({
        id: 'qa-direction',
        options: [{ value: 'expense', label: '支出' }, { value: 'income', label: '收入' }],
        selected: 'expense',
        className: 'pselect-block pselect-theme-ledger ledger-qa-direction',
    });
    return `
        <div class="ledger-quick-add" id="ledger-quick-add">
            ${dirSelect}
            <input type="number" class="ledger-qa-amount"   id="qa-amount"   placeholder="金额" step="0.01" min="0">
            <input type="text"   class="ledger-qa-title"    id="qa-title"    placeholder="摘要">
            <input type="text"   class="ledger-qa-category" id="qa-category" placeholder="分类（其他）">
            <input type="text"   class="ledger-qa-date"     id="qa-date"     value="${today}" inputmode="numeric" placeholder="YYYY-MM-DD">
            <button class="ledger-qa-submit" id="qa-submit">+ 记录</button>
        </div>`;
}

function renderFilterBar() {
    const dateOptions = [
        { value: 'today',  label: '今天' },
        { value: 'week',   label: '本周' },
        { value: 'month',  label: '本月' },
        { value: 'quarter', label: '本季' },
        { value: 'year',   label: '今年' },
        { value: 'last_year', label: '去年' },
        { value: 'custom', label: '自定义' },
        { value: 'all',    label: '全部' },
    ];
    const dirOptions = [
        { value: '',        label: '全部方向' },
        { value: 'expense', label: '支出' },
        { value: 'income',  label: '收入' },
    ];
    const catOptions = [
        { value: '', label: '全部分类' },
        ..._allCategories.map(c => ({ value: c, label: c })),
    ];

    const customVisible = _dateFilter === 'custom' ? '' : 'display:none;';

    return `
        <div class="ledger-filter-bar" id="ledger-filter-bar">
            <div class="ledger-filter-item ledger-filter-item--date">
                <label>时段：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-date', options: dateOptions, selected: _dateFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-date' })}
                    <div class="ledger-filter-range" id="filter-custom-range" style="${customVisible}">
                        <input type="text" class="ledger-custom-date-input" id="filter-date-start" value="${_customDateStart}" inputmode="numeric" placeholder="YYYY-MM-DD">
                        <span class="ledger-filter-range-sep">至</span>
                        <input type="text" class="ledger-custom-date-input" id="filter-date-end" value="${_customDateEnd}" inputmode="numeric" placeholder="YYYY-MM-DD">
                        <button type="button" class="ledger-range-apply" id="filter-date-apply">应用</button>
                    </div>
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--direction">
                <label>方向：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-direction', options: dirOptions, selected: _directionFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-direction' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--category">
                <label>分类：</label>
                <div class="ledger-filter-controls">
                    ${renderCustomSelect({ id: 'filter-category', options: catOptions, selected: _categoryFilter, className: 'pselect-block pselect-theme-ledger ledger-filter-category' })}
                </div>
            </div>
            <div class="ledger-filter-item ledger-filter-item--amount">
                <label>金额：</label>
                <div class="ledger-filter-controls">
                    <input type="number" class="ledger-amount-input" id="filter-amount-min"
                        placeholder="最小" min="0" step="0.01" value="${_amountMin}">
                    <span style="font-size:12px;color:var(--color-text-secondary);">~</span>
                    <input type="number" class="ledger-amount-input" id="filter-amount-max"
                        placeholder="最大" min="0" step="0.01" value="${_amountMax}">
                </div>
            </div>
        </div>`;
}

function renderInsights() {
    return renderLedgerInsightsPanel(_insightsData);
}

function currentRangeLabel() {
    const labels = {
        today: '今天',
        week: '本周',
        month: '本月',
        quarter: '本季',
        year: '今年',
        last_year: '去年',
        all: '全部',
        custom: '自定义范围',
    };
    return labels[_dateFilter] || '当前范围';
}

function renderItemRow(item) {
    const isIncome = item.direction === 'income';
    const dirIcon  = isIncome ? '⬆️' : '⬇️';
    const amtColor = isIncome ? 'var(--color-success)' : 'var(--color-ledger)';
    const amtSign  = isIncome ? '+' : '-';
    const meta     = _sortMode === 'amount' && item.ledger_date
        ? `<span class="ledger-row-meta">${item.ledger_date}</span>`
        : '';
    const catBadge = item.ledger_category
        ? `<span class="badge" style="font-size:11px;">${item.ledger_category}</span>`
        : '';
    return `
        <div class="ledger-row" data-id="${item.id}">
            <span class="ledger-dir-icon">${dirIcon}</span>
            <div class="ledger-row-main">
                <span class="ledger-row-title" title="${item.title || ''}">${item.title || '(无摘要)'}</span>
                ${meta}
            </div>
            ${catBadge}
            <span class="ledger-row-amount" style="color:${amtColor};">${amtSign}${formatAmount(item.amount)}</span>
            <div class="ledger-row-actions">
                <button class="btn btn-icon btn-sm btn-edit-ledger"   data-id="${item.id}" title="编辑">✏️</button>
                <button class="btn btn-icon btn-sm btn-delete-ledger" data-id="${item.id}" title="删除">🗑️</button>
            </div>
        </div>`;
}

function renderListToolbar() {
    const sortHint = _sortMode === 'amount'
        ? '当前筛选条件下按金额从高到低'
        : '当前筛选条件下按日期从新到旧';
    return `
        <div class="ledger-list-toolbar">
            <div class="ledger-list-toolbar-copy">
                <strong>账目明细</strong>
                <span>${sortHint}</span>
            </div>
            <div class="ledger-sort-toggle" role="tablist" aria-label="记账排序">
                <button class="ledger-sort-btn${_sortMode === 'date' ? ' is-active' : ''}" id="ledger-sort-date" type="button">按时间</button>
                <button class="ledger-sort-btn${_sortMode === 'amount' ? ' is-active' : ''}" id="ledger-sort-amount" type="button">按金额</button>
            </div>
        </div>`;
}

function renderList(items) {
    if (items.length === 0) return `<div class="ledger-empty">暂无记录</div>`;
    if (_sortMode === 'amount') {
        return `<div class="ledger-flat-list">${items.map(renderItemRow).join('')}</div>`;
    }
    const groups = groupByDate(items);
    return groups.map(g => `
        <div class="ledger-date-group">
            <div class="ledger-date-group-header">${g.date}</div>
            ${g.items.map(renderItemRow).join('')}
        </div>`).join('');
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
                        <span class="ledger-hero-tag">${currentRangeLabel()}</span>
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
                ${renderInsights()}
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
            page:     _page,
            pageSize: PAGE_SIZE,
            total:    _total,
            onChange: async (newPage) => {
                _page = newPage;
                await loadAndRender();
            },
        });
    }

    attachListeners();
}

// ── listeners ─────────────────────────────────────────────────────────────────

let _amountDebounceTimer = null;

function attachListeners() {
    if (!_container) return;

    // Quick-add submit
    const qaSubmit = _container.querySelector('#qa-submit');
    if (qaSubmit) qaSubmit.addEventListener('click', handleQuickAdd);

    ['qa-amount', 'qa-title', 'qa-category'].forEach(id => {
        const el = _container.querySelector(`#${id}`);
        if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') handleQuickAdd(); });
    });

    // Custom selects — filter bar + quick-add
    initCustomSelects(_container, {
        'qa-direction':     () => {}, // no state change needed; value read on submit
        'filter-date':      async (val) => {
            _dateFilter = val;
            _page = 1;
            if (val === 'custom') {
                // Pre-fill with last 30 days so inputs show dates instead of 年/月/日
                if (!_customDateStart || !_customDateEnd) {
                    const r = dateRangeForFilter('month');
                    _customDateStart = r.start_date;
                    _customDateEnd   = r.end_date;
                }
                await loadAndRender();
            } else {
                await loadAndRender();
            }
        },
        'filter-direction': async (val) => { _directionFilter = val; _page = 1; await loadAndRender(); },
        'filter-category':  async (val) => { _categoryFilter = val;  _page = 1; await loadAndRender(); },
    });

    // Custom date range inputs
    const dateStart = _container.querySelector('#filter-date-start');
    const dateEnd   = _container.querySelector('#filter-date-end');
    const dateApply = _container.querySelector('#filter-date-apply');
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
    if (dateStart) {
        dateStart.addEventListener('keydown', async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomDateRange();
            }
        });
    }
    if (dateEnd) {
        dateEnd.addEventListener('keydown', async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomDateRange();
            }
        });
    }
    if (dateApply) dateApply.addEventListener('click', applyCustomDateRange);

    // Amount range inputs — debounced
    ['filter-amount-min', 'filter-amount-max'].forEach(id => {
        const el = _container.querySelector(`#${id}`);
        if (!el) return;
        el.addEventListener('input', () => {
            clearTimeout(_amountDebounceTimer);
            _amountDebounceTimer = setTimeout(async () => {
                _amountMin = _container.querySelector('#filter-amount-min')?.value ?? '';
                _amountMax = _container.querySelector('#filter-amount-max')?.value ?? '';
                _page = 1;
                await loadAndRender();
            }, 600);
        });
    });

    ['ledger-sort-date', 'ledger-sort-amount'].forEach(id => {
        const btn = _container.querySelector(`#${id}`);
        if (!btn) return;
        btn.addEventListener('click', async () => {
            const nextMode = id === 'ledger-sort-amount' ? 'amount' : 'date';
            if (_sortMode === nextMode) return;
            _sortMode = nextMode;
            _page = 1;
            await loadAndRender();
        });
    });

    // Edit / Delete / row-click via delegation
    const list = _container.querySelector('#ledger-list');
    if (list) {
        list.addEventListener('click', async e => {
            const editBtn   = e.target.closest('.btn-edit-ledger');
            const deleteBtn = e.target.closest('.btn-delete-ledger');
            if (editBtn) {
                const item = _items.find(i => String(i.id) === editBtn.dataset.id);
                if (item) openEditModal(item);
            } else if (deleteBtn) {
                await handleDelete(deleteBtn.dataset.id);
            } else {
                const row = e.target.closest('.ledger-row');
                if (row) {
                    const item = _items.find(i => String(i.id) === row.dataset.id);
                    if (item) openDetailModal(item);
                }
            }
        });
    }
}

// ── quick-add ─────────────────────────────────────────────────────────────────

async function handleQuickAdd() {
    if (!_container) return;

    const dirCsel   = _container.querySelector('#qa-direction');
    const direction = dirCsel?.dataset.value || 'expense';
    const amountVal = _container.querySelector('#qa-amount').value.trim();
    const title     = _container.querySelector('#qa-title').value.trim();
    const category  = _container.querySelector('#qa-category').value.trim() || '其他';
    const dateVal   = _container.querySelector('#qa-date').value;

    if (!amountVal || isNaN(parseFloat(amountVal)) || parseFloat(amountVal) <= 0) {
        showToast('请填写有效金额', 'warning');
        return;
    }
    if (!title) {
        showToast('请填写摘要', 'warning');
        return;
    }
    if (!isValidDateInput(dateVal)) {
        showToast('请填写有效日期，格式为 YYYY-MM-DD', 'warning');
        return;
    }

    try {
        await api.post('/items', {
            type:            'ledger',
            direction,
            amount:          parseFloat(amountVal),
            title,
            ledger_category: category,
            ledger_date:     dateVal || todayStr(),
        });
        showToast('记录已添加', 'success');
        _container.querySelector('#qa-amount').value = '';
        _container.querySelector('#qa-title').value  = '';
        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        _page = 1;
        await loadAndRender(true);
    } catch (err) {
        showToast('添加失败：' + err.message, 'error');
    }
}

// ── detail modal ──────────────────────────────────────────────────────────────

export function openDetailModal(item) {
    const isIncome = item.direction === 'income';
    const amtColor = isIncome ? 'var(--color-success)' : 'var(--color-ledger)';
    const amtSign  = isIncome ? '+' : '-';
    const dirLabel = isIncome ? '收入' : '支出';
    const rows = [
        ['摘要', item.title],
        ['分类', item.ledger_category],
        ['日期', item.ledger_date],
        ['备注', item.remark],
    ].filter(([, value]) => value);

    const rowsHtml = rows.map(([label, value], index) => {
        const isLast = index === rows.length - 1;
        return `
            <div style="display:flex;gap:12px;padding:12px 0;${isLast ? '' : 'border-bottom:1px solid var(--color-border);'}">
                <span style="width:56px;flex-shrink:0;font-size:12px;color:var(--color-text-secondary);padding-top:2px;">${label}</span>
                <span style="flex:1;font-size:14px;color:var(--color-text);word-break:break-word;line-height:1.6;">${value}</span>
            </div>`;
    }).join('');

    const body = `
        <div style="margin:-2px 0;">
            <div style="display:flex;align-items:baseline;gap:8px;padding-bottom:16px;border-bottom:1px solid var(--color-border);margin-bottom:14px;">
                <span style="font-size:28px;font-weight:700;color:${amtColor};">${amtSign}${formatAmount(item.amount)}</span>
                <span style="font-size:13px;padding:2px 8px;border-radius:20px;background:${isIncome ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)'};color:${amtColor};font-weight:600;">${dirLabel}</span>
            </div>
            <div style="padding:0 14px;background:var(--color-bg);border:1px solid var(--color-border);border-radius:12px;">
                ${rowsHtml}
            </div>
        </div>`;

    const footer = `
        <button class="btn btn-secondary" id="detail-close">关闭</button>
        <button class="btn btn-primary"   id="detail-edit">编辑</button>`;

    const content = showModal(item.title || '记录详情', body, { footer });
    content.querySelector('#detail-close').onclick = closeModal;
    content.querySelector('#detail-edit').onclick  = () => { closeModal(); openEditModal(item); };
}

// ── edit modal ────────────────────────────────────────────────────────────────

function openEditModal(existing) {
    const fields = LEDGER_FIELDS.map(f => ({
        ...f,
        value: existing[f.name] !== undefined && existing[f.name] !== null
            ? String(existing[f.name]) : (f.value ?? ''),
    }));

    const bodyHTML = `<form id="ledger-edit-form">${buildFormHTML(fields)}</form>`;
    const footer = `
        <button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary" id="modal-cancel">取消</button>
        <button class="btn btn-primary"   id="modal-save">保存</button>`;

    const content = showModal('编辑记录', bodyHTML, { footer });
    initFormInteractions(content);

    content.querySelector('#modal-cancel').onclick = closeModal;
    content.querySelector('#modal-delete').onclick = async () => {
        closeModal();
        await handleDelete(existing.id);
    };
    content.querySelector('#modal-save').onclick = async () => {
        const form = content.querySelector('#ledger-edit-form');
        const data = getFormData(form);
        if (!data.title) { showToast('请填写摘要', 'warning'); return; }
        if (!data.amount || data.amount <= 0) { showToast('请填写有效金额', 'warning'); return; }
        try {
            await api.put('/items/' + existing.id, data);
            showToast('记录已更新', 'success');
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await loadAndRender(true);
        } catch (err) {
            showToast('更新失败：' + err.message, 'error');
        }
    };
}

// ── delete ────────────────────────────────────────────────────────────────────

async function handleDelete(id) {
    const item = _items.find(entry => String(entry.id) === String(id));
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
        await api.delete('/items/' + id);
        showToast('记录已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        await loadAndRender(true);
    } catch (err) {
        showToast('删除失败：' + err.message, 'error');
    }
}

// ── data load ─────────────────────────────────────────────────────────────────

async function loadAndRender(refreshCategories = false) {
    try {
        const fetches = [fetchItems(_page), fetchAggregate(), fetchInsights().catch(() => null)];
        if (refreshCategories) fetches.push(fetchCategories());
        const results = await Promise.all(fetches);
        _items       = results[0].items;
        _total       = results[0].total;
        _summaryData = results[1];
        _insightsData = results[2];
        if (refreshCategories) _allCategories = results[3];
    } catch (err) {
        _items       = [];
        _total       = 0;
        _summaryData = { income: 0, expense: 0, balance: 0, count: 0 };
        _insightsData = null;
        showToast('加载账本失败：' + err.message, 'error');
    }
    renderPage();
}

// ── page module exports ───────────────────────────────────────────────────────

export async function render(container) {
    _container       = container;
    _items           = [];
    _total           = 0;
    _page            = 1;
    _sortMode        = 'date';
    _summaryData     = { income: 0, expense: 0, balance: 0, count: 0 };
    _insightsData    = null;

    renderPage(); // immediate skeleton

    _allCategories = await fetchCategories();
    await loadAndRender();

    _dataChangedHandler = (event) => {
        const changedType = event?.detail?.type;
        if (changedType && changedType !== 'ledger') return;
        loadAndRender(true);
    };
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
    _items     = [];
    _total     = 0;
}

export function onRouteEnter(_params) {}
