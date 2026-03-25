import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';

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
let _dateFilter         = 'month';   // 'today' | 'week' | 'month' | 'year' | 'all' | 'custom'
let _directionFilter    = '';        // '' | 'income' | 'expense'
let _categoryFilter     = '';
let _amountMin          = '';
let _amountMax          = '';
let _customDateStart    = '';
let _customDateEnd      = '';
let _summaryData        = { income: 0, expense: 0, balance: 0, count: 0 };
let _allCategories      = [];
let _dataChangedHandler = null;
let _docClickAttached   = false;

// ── date helpers ──────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${padZ(d.getMonth() + 1)}-${padZ(d.getDate())}`;
}

function dateRangeForFilter(filter) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    if (filter === 'today') {
        const s = todayStr();
        return { start_date: s, end_date: s };
    }
    if (filter === 'week') {
        const start = new Date(today);
        start.setDate(today.getDate() - 6);
        return {
            start_date: `${start.getFullYear()}-${padZ(start.getMonth() + 1)}-${padZ(start.getDate())}`,
            end_date:   todayStr(),
        };
    }
    if (filter === 'month') {
        const start = new Date(today);
        start.setDate(today.getDate() - 29);
        return {
            start_date: `${start.getFullYear()}-${padZ(start.getMonth() + 1)}-${padZ(start.getDate())}`,
            end_date:   todayStr(),
        };
    }
    if (filter === 'year') {
        return {
            start_date: `${today.getFullYear()}-01-01`,
            end_date:   todayStr(),
        };
    }
    if (filter === 'custom') {
        if (_customDateStart && _customDateEnd) {
            return { start_date: _customDateStart, end_date: _customDateEnd };
        }
        return {};
    }
    // 'all'
    return {};
}

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtAmount(amount) {
    return '¥' + Number(amount).toFixed(2);
}

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
        sort:       'ledger_date',
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

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .ledger-page { padding: 24px; max-width: 900px; margin: 0 auto; }

        .ledger-page-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 12px;
        }

        /* Summary cards */
        .ledger-summary-cards {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin-bottom: 20px;
        }
        @media (max-width: 600px) {
            .ledger-summary-cards { grid-template-columns: 1fr; }
        }
        .ledger-summary-card {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 16px 20px;
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .ledger-summary-icon {
            font-size: 28px;
            flex-shrink: 0;
        }
        .ledger-summary-value {
            font-size: 20px;
            font-weight: 700;
            line-height: 1.2;
        }
        .ledger-summary-label {
            font-size: 12px;
            color: var(--color-text-secondary);
            margin-top: 2px;
        }

        /* Quick-add bar */
        .ledger-quick-add {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 12px 16px;
            margin-bottom: 16px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }
        .ledger-quick-add input {
            font-size: 13px;
            height: 34px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--color-border);
        }
        .ledger-quick-add input:focus {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.1);
            outline: none;
        }
        .ledger-qa-amount    { width: 110px; }
        .ledger-qa-title     { flex: 1; min-width: 100px; }
        .ledger-qa-category  { width: 90px; }
        .ledger-qa-date      { width: 140px; }
        .ledger-qa-submit {
            height: 34px;
            padding: 0 14px;
            font-size: 13px;
            flex-shrink: 0;
            background: var(--color-ledger);
            color: #fff;
            border: none;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-weight: 600;
            transition: background .15s;
        }
        .ledger-qa-submit:hover { background: #dc2626; }

        /* Filter bar */
        .ledger-filter-bar {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 10px 14px;
            margin-bottom: 16px;
        }
        .ledger-filter-bar label {
            font-size: 12px;
            font-weight: 500;
            color: var(--color-text-secondary);
            white-space: nowrap;
        }
        .ledger-filter-group {
            display: flex;
            gap: 6px;
            align-items: center;
            white-space: nowrap;
        }
        .ledger-filter-sep {
            width: 1px;
            height: 18px;
            background: var(--color-border);
            flex-shrink: 0;
        }
        .ledger-amount-input {
            height: 30px;
            width: 80px;
            font-size: 13px;
            padding: 0 8px;
            border: 1px solid var(--color-border);
            border-radius: 20px;
            background: var(--color-bg);
            color: var(--color-text);
            font-weight: 500;
            outline: none;
            transition: border-color .15s, box-shadow .15s;
        }
        .ledger-amount-input:focus {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }
        .ledger-amount-input::placeholder { color: var(--color-text-tertiary); }
        .ledger-custom-date-input {
            height: 30px;
            font-size: 13px;
            padding: 0 8px;
            border: 1px solid var(--color-border);
            border-radius: 8px;
            background: var(--color-bg);
            color: var(--color-text);
            outline: none;
            transition: border-color .15s;
        }
        .ledger-custom-date-input:focus {
            border-color: var(--color-ledger);
        }

        /* Custom select ── shared base */
        .csel {
            position: relative;
            display: inline-block;
            flex-shrink: 0;
        }
        .csel-trigger {
            display: flex;
            align-items: center;
            gap: 5px;
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
            border: 1px solid var(--color-border);
            background: var(--color-bg);
            color: var(--color-text);
            font-size: 13px;
            font-weight: 500;
            transition: border-color .15s, background .15s, box-shadow .15s;
        }
        .csel-trigger:hover {
            border-color: #9CA3AF;
            background: #EEF0F2;
        }
        .csel.csel-open .csel-trigger {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }
        .csel-chevron {
            flex-shrink: 0;
            color: #9CA3AF;
            transition: transform .2s;
        }
        .csel.csel-open .csel-chevron {
            transform: rotate(180deg);
        }
        .csel-panel {
            display: none;
            position: absolute;
            top: calc(100% + 5px);
            left: 0;
            min-width: 100%;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: 10px;
            box-shadow: 0 6px 24px rgba(0,0,0,0.13);
            z-index: 1000;
            padding: 4px 0;
            overflow: hidden;
        }
        .csel.csel-open .csel-panel {
            display: block;
        }
        .csel-option {
            padding: 7px 16px;
            font-size: 13px;
            cursor: pointer;
            white-space: nowrap;
            color: var(--color-text);
            transition: background .1s;
        }
        .csel-option:hover {
            background: rgba(0,0,0,0.04);
        }
        .csel-option.csel-selected {
            font-weight: 600;
            color: var(--color-ledger);
            background: rgba(239,68,68,0.05);
        }

        /* Filter-bar pill selects */
        .csel-filter .csel-trigger {
            height: 30px;
            padding: 0 10px 0 12px;
            border-radius: 20px;
            border-color: rgba(239,68,68,0.35);
            color: #b91c1c;
        }
        .csel-filter .csel-trigger:hover {
            border-color: var(--color-ledger);
            background: rgba(239,68,68,0.06);
        }
        .csel-filter.csel-open .csel-trigger {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }
        .csel-filter .csel-chevron { color: var(--color-ledger); }

        /* Quick-add direction select */
        .csel-qa-dir .csel-trigger {
            height: 34px;
            padding: 0 10px 0 13px;
            border-radius: 20px;
            border-color: rgba(239,68,68,0.35);
            color: #b91c1c;
            font-weight: 600;
        }
        .csel-qa-dir .csel-trigger:hover {
            border-color: var(--color-ledger);
            background: rgba(239,68,68,0.06);
        }
        .csel-qa-dir.csel-open .csel-trigger {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.12);
        }
        .csel-qa-dir .csel-chevron { color: var(--color-ledger); }

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
        .ledger-row:last-child { border-bottom: none; }
        .ledger-row:hover { background: var(--color-hover, rgba(0,0,0,0.02)); }
        .ledger-dir-icon {
            font-size: 16px;
            flex-shrink: 0;
            width: 22px;
            text-align: center;
        }
        .ledger-row-title {
            flex: 1;
            font-size: 14px;
            font-weight: 500;
            color: var(--color-text);
            overflow: hidden;
            text-overflow: ellipsis;
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
        .ledger-empty {
            text-align: center;
            padding: 40px 0;
            color: var(--color-text-tertiary);
            font-size: 14px;
        }
        .ledger-pagination { margin-top: 16px; }
    `;
    document.head.appendChild(style);
}

// ── custom select component ───────────────────────────────────────────────────

const CHEVRON_SVG = `<svg class="csel-chevron" width="13" height="13" viewBox="0 0 13 13" fill="none">
    <path d="M3 5l3.5 3.5L10 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

function renderCustomSelect({ id, options, selected, className = '' }) {
    const cur = options.find(o => String(o.value) === String(selected)) || options[0];
    const optHtml = options.map(o => {
        const sel = String(o.value) === String(selected) ? ' csel-selected' : '';
        return `<div class="csel-option${sel}" data-value="${o.value}">${o.label}</div>`;
    }).join('');
    return `
        <div class="csel ${className}" id="${id}" data-value="${selected}">
            <div class="csel-trigger">
                <span class="csel-label">${cur ? cur.label : ''}</span>
                ${CHEVRON_SVG}
            </div>
            <div class="csel-panel">${optHtml}</div>
        </div>`;
}

function initCustomSelects(container, callbacks) {
    if (!_docClickAttached) {
        document.addEventListener('click', () => {
            document.querySelectorAll('.csel.csel-open').forEach(el => el.classList.remove('csel-open'));
        });
        _docClickAttached = true;
    }

    container.querySelectorAll('.csel').forEach(csel => {
        const trigger = csel.querySelector('.csel-trigger');
        const panel   = csel.querySelector('.csel-panel');

        trigger.addEventListener('click', e => {
            e.stopPropagation();
            const wasOpen = csel.classList.contains('csel-open');
            document.querySelectorAll('.csel.csel-open').forEach(el => el.classList.remove('csel-open'));
            if (!wasOpen) csel.classList.add('csel-open');
        });

        panel.addEventListener('click', e => {
            const opt = e.target.closest('.csel-option');
            if (!opt) return;
            const value = opt.dataset.value;
            csel.dataset.value = value;
            csel.querySelector('.csel-label').textContent = opt.textContent.trim();
            panel.querySelectorAll('.csel-option').forEach(o =>
                o.classList.toggle('csel-selected', o.dataset.value === value)
            );
            csel.classList.remove('csel-open');
            const cb = callbacks[csel.id];
            if (cb) cb(value);
        });
    });
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
                    <div class="ledger-summary-value" style="color:var(--color-success);">${fmtAmount(income)}</div>
                    <div class="ledger-summary-label">收入</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">📉</div>
                <div>
                    <div class="ledger-summary-value" style="color:var(--color-ledger);">${fmtAmount(expense)}</div>
                    <div class="ledger-summary-label">支出</div>
                </div>
            </div>
            <div class="ledger-summary-card">
                <div class="ledger-summary-icon">💰</div>
                <div>
                    <div class="ledger-summary-value" style="color:${balanceColor};">${fmtAmount(balance)}</div>
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
        className: 'csel-qa-dir',
    });
    return `
        <div class="ledger-quick-add" id="ledger-quick-add">
            ${dirSelect}
            <input type="number" class="ledger-qa-amount"   id="qa-amount"   placeholder="金额" step="0.01" min="0">
            <input type="text"   class="ledger-qa-title"    id="qa-title"    placeholder="摘要">
            <input type="text"   class="ledger-qa-category" id="qa-category" placeholder="分类（其他）">
            <input type="date"   class="ledger-qa-date"     id="qa-date"     value="${today}">
            <button class="ledger-qa-submit" id="qa-submit">+ 记录</button>
        </div>`;
}

function renderFilterBar() {
    const dateOptions = [
        { value: 'today',  label: '今天' },
        { value: 'week',   label: '近7天' },
        { value: 'month',  label: '近30天' },
        { value: 'year',   label: '今年' },
        { value: 'all',    label: '全部' },
        { value: 'custom', label: '自定义' },
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
            <div class="ledger-filter-group">
                <label>时段：</label>
                ${renderCustomSelect({ id: 'filter-date', options: dateOptions, selected: _dateFilter, className: 'csel-filter' })}
            </div>
            <div class="ledger-filter-group" id="filter-custom-range" style="${customVisible}gap:6px;align-items:center;">
                <input type="date" class="ledger-custom-date-input" id="filter-date-start" value="${_customDateStart}">
                <span style="font-size:12px;color:var(--color-text-secondary);">—</span>
                <input type="date" class="ledger-custom-date-input" id="filter-date-end"   value="${_customDateEnd}">
            </div>
            <div class="ledger-filter-sep"></div>
            <div class="ledger-filter-group">
                <label>方向：</label>
                ${renderCustomSelect({ id: 'filter-direction', options: dirOptions, selected: _directionFilter, className: 'csel-filter' })}
            </div>
            <div class="ledger-filter-sep"></div>
            <div class="ledger-filter-group">
                <label>分类：</label>
                ${renderCustomSelect({ id: 'filter-category', options: catOptions, selected: _categoryFilter, className: 'csel-filter' })}
            </div>
            <div class="ledger-filter-sep"></div>
            <div class="ledger-filter-group">
                <label>金额：</label>
                <input type="number" class="ledger-amount-input" id="filter-amount-min"
                    placeholder="最小" min="0" step="0.01" value="${_amountMin}">
                <span style="font-size:12px;color:var(--color-text-secondary);">~</span>
                <input type="number" class="ledger-amount-input" id="filter-amount-max"
                    placeholder="最大" min="0" step="0.01" value="${_amountMax}">
            </div>
        </div>`;
}

function renderItemRow(item) {
    const isIncome = item.direction === 'income';
    const dirIcon  = isIncome ? '⬆️' : '⬇️';
    const amtColor = isIncome ? 'var(--color-success)' : 'var(--color-ledger)';
    const amtSign  = isIncome ? '+' : '-';
    const catBadge = item.ledger_category
        ? `<span class="badge" style="font-size:11px;">${item.ledger_category}</span>`
        : '';
    return `
        <div class="ledger-row" data-id="${item.id}">
            <span class="ledger-dir-icon">${dirIcon}</span>
            <span class="ledger-row-title" title="${item.title || ''}">${item.title || '(无摘要)'}</span>
            ${catBadge}
            <span class="ledger-row-amount" style="color:${amtColor};">${amtSign}${fmtAmount(item.amount)}</span>
            <div class="ledger-row-actions">
                <button class="btn btn-icon btn-sm btn-edit-ledger"   data-id="${item.id}" title="编辑">✏️</button>
                <button class="btn btn-icon btn-sm btn-delete-ledger" data-id="${item.id}" title="删除">🗑️</button>
            </div>
        </div>`;
}

function renderList(items) {
    if (items.length === 0) return `<div class="ledger-empty">暂无记录</div>`;
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
            <div class="ledger-page-header">
                <div>
                    <h2 style="font-size:20px;font-weight:700;color:var(--color-ledger);">💰 账本</h2>
                    <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">记录你的收支明细</p>
                </div>
            </div>
            ${renderSummaryCards()}
            ${renderQuickAdd()}
            ${renderFilterBar()}
            <div class="card">
                <div id="ledger-list">${renderList(_items)}</div>
                <div id="ledger-pagination" class="ledger-pagination"></div>
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
            const group = _container.querySelector('#filter-custom-range');
            if (group) group.style.display = val === 'custom' ? 'flex' : 'none';
            if (val !== 'custom') await loadAndRender();
        },
        'filter-direction': async (val) => { _directionFilter = val; _page = 1; await loadAndRender(); },
        'filter-category':  async (val) => { _categoryFilter = val;  _page = 1; await loadAndRender(); },
    });

    // Custom date range inputs
    const dateStart = _container.querySelector('#filter-date-start');
    const dateEnd   = _container.querySelector('#filter-date-end');
    if (dateStart) {
        dateStart.addEventListener('change', async () => {
            _customDateStart = dateStart.value;
            if (_customDateStart && _customDateEnd) { _page = 1; await loadAndRender(); }
        });
    }
    if (dateEnd) {
        dateEnd.addEventListener('change', async () => {
            _customDateEnd = dateEnd.value;
            if (_customDateStart && _customDateEnd) { _page = 1; await loadAndRender(); }
        });
    }

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

    // Edit / Delete via delegation
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
            await loadAndRender();
        } catch (err) {
            showToast('更新失败：' + err.message, 'error');
        }
    };
}

// ── delete ────────────────────────────────────────────────────────────────────

async function handleDelete(id) {
    if (!window.confirm('确定要删除这条记录吗？')) return;
    try {
        await api.delete('/items/' + id);
        showToast('记录已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        await loadAndRender();
    } catch (err) {
        showToast('删除失败：' + err.message, 'error');
    }
}

// ── data load ─────────────────────────────────────────────────────────────────

async function loadAndRender(refreshCategories = false) {
    try {
        const fetches = [fetchItems(_page), fetchAggregate()];
        if (refreshCategories) fetches.push(fetchCategories());
        const results = await Promise.all(fetches);
        _items       = results[0].items;
        _total       = results[0].total;
        _summaryData = results[1];
        if (refreshCategories) _allCategories = results[2];
    } catch (err) {
        _items       = [];
        _total       = 0;
        _summaryData = { income: 0, expense: 0, balance: 0, count: 0 };
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
    _summaryData     = { income: 0, expense: 0, balance: 0, count: 0 };

    renderPage(); // immediate skeleton

    _allCategories = await fetchCategories();
    await loadAndRender();

    _dataChangedHandler = () => loadAndRender();
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
