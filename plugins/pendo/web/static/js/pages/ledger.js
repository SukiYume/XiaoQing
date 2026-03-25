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
let _dateFilter         = 'month';   // 'today' | 'week' | 'month' | 'all'
let _directionFilter    = '';        // '' | 'income' | 'expense'
let _categoryFilter     = '';
let _dataChangedHandler = null;

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
        const start = todayStr();
        const end   = todayStr();
        return { start_date: start, end_date: end };
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
    // 'all'
    return {};
}

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtAmount(amount) {
    return '¥' + Number(amount).toFixed(2);
}

function getCategories() {
    const cats = new Set();
    _items.forEach(item => { if (item.ledger_category) cats.add(item.ledger_category); });
    return Array.from(cats).sort();
}

function computeSummary(items) {
    let income = 0, expense = 0;
    items.forEach(item => {
        const amt = parseFloat(item.amount) || 0;
        if (item.direction === 'income')  income  += amt;
        if (item.direction === 'expense') expense += amt;
    });
    return { income, expense, balance: income - expense };
}

function groupByDate(items) {
    const groups = {};
    items.forEach(item => {
        const d = item.ledger_date || '未知日期';
        if (!groups[d]) groups[d] = [];
        groups[d].push(item);
    });
    // Sort dates descending
    const sorted = Object.keys(groups).sort((a, b) => b.localeCompare(a));
    return sorted.map(date => ({ date, items: groups[date] }));
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
        .ledger-quick-add select,
        .ledger-quick-add input {
            font-size: 13px;
            height: 34px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--color-border);
        }
        .ledger-quick-add select:focus,
        .ledger-quick-add input:focus {
            border-color: var(--color-ledger);
            box-shadow: 0 0 0 3px rgba(239,68,68,0.1);
            outline: none;
        }
        .ledger-qa-direction { width: 90px; flex-shrink: 0; }
        .ledger-qa-amount    { width: 110px; }
        .ledger-qa-title     { flex: 1; min-width: 100px; }
        .ledger-qa-category  { width: 90px; }
        .ledger-qa-date      { width: 140px; }

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
        .ledger-filter-bar select {
            font-size: 13px;
            height: 30px;
            padding: 0 30px 0 12px;
            border-radius: 20px;
            border: 1px solid var(--color-border);
            background-position: right 9px center;
            font-weight: 500;
            min-width: 80px;
        }
        .ledger-filter-group {
            display: flex;
            gap: 6px;
            align-items: center;
            white-space: nowrap;
        }

        /* Date group list */
        .ledger-date-group {
            margin-bottom: 12px;
        }
        .ledger-date-group-header {
            font-size: 13px;
            font-weight: 600;
            color: var(--color-text-secondary);
            padding: 6px 0 4px;
            border-bottom: 1px solid var(--color-border);
            margin-bottom: 0;
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
        .ledger-pagination {
            margin-top: 16px;
        }
    `;
    document.head.appendChild(style);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchItems(page) {
    const params = {
        type:       'ledger',
        date_field: 'ledger_date',
        page,
        page_size:  PAGE_SIZE,
    };
    const range = dateRangeForFilter(_dateFilter);
    if (range.start_date) params.start_date = range.start_date;
    if (range.end_date)   params.end_date   = range.end_date;
    if (_directionFilter) params.direction = _directionFilter;
    if (_categoryFilter)  params.category = _categoryFilter;

    const res = await api.get('/items', params);
    return {
        items: (res.data && res.data.items) ? res.data.items : [],
        total: (res.data && res.data.total != null) ? res.data.total : 0,
    };
}

// ── render ────────────────────────────────────────────────────────────────────

function renderSummaryCards(items) {
    const { income, expense, balance } = computeSummary(items);
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
        </div>
    `;
}

function renderQuickAdd() {
    const today = todayStr();
    return `
        <div class="ledger-quick-add" id="ledger-quick-add">
            <select class="ledger-qa-direction" id="qa-direction">
                <option value="expense">支出</option>
                <option value="income">收入</option>
            </select>
            <input type="number" class="ledger-qa-amount"   id="qa-amount"   placeholder="金额" step="0.01" min="0">
            <input type="text"   class="ledger-qa-title"    id="qa-title"    placeholder="摘要">
            <input type="text"   class="ledger-qa-category" id="qa-category" placeholder="分类（其他）">
            <input type="date"   class="ledger-qa-date"     id="qa-date"     value="${today}">
            <button class="btn btn-primary btn-sm" id="qa-submit">+ 记录</button>
        </div>
    `;
}

function renderFilterBar() {
    const dateOptions = [
        { value: 'today', label: '今天' },
        { value: 'week',  label: '近7天' },
        { value: 'month', label: '近30天' },
        { value: 'all',   label: '全部' },
    ];
    const dirOptions = [
        { value: '',        label: '全部方向' },
        { value: 'expense', label: '支出' },
        { value: 'income',  label: '收入' },
    ];
    const categories  = getCategories();
    const catOptions  = ['', ...categories];

    const dateSelect = dateOptions
        .map(o => `<option value="${o.value}"${_dateFilter === o.value ? ' selected' : ''}>${o.label}</option>`)
        .join('');
    const dirSelect = dirOptions
        .map(o => `<option value="${o.value}"${_directionFilter === o.value ? ' selected' : ''}>${o.label}</option>`)
        .join('');
    const catSelect = catOptions
        .map(c => `<option value="${c}"${_categoryFilter === c ? ' selected' : ''}>${c || '全部分类'}</option>`)
        .join('');

    return `
        <div class="ledger-filter-bar">
            <div class="ledger-filter-group">
                <label>时段：</label>
                <select id="filter-date">${dateSelect}</select>
            </div>
            <div class="ledger-filter-group">
                <label>方向：</label>
                <select id="filter-direction">${dirSelect}</select>
            </div>
            <div class="ledger-filter-group">
                <label>分类：</label>
                <select id="filter-category">${catSelect}</select>
            </div>
        </div>
    `;
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
        </div>
    `;
}

function renderList(items) {
    if (items.length === 0) {
        return `<div class="ledger-empty">暂无记录</div>`;
    }
    const groups = groupByDate(items);
    return groups.map(g => `
        <div class="ledger-date-group">
            <div class="ledger-date-group-header">${g.date}</div>
            ${g.items.map(renderItemRow).join('')}
        </div>
    `).join('');
}

function renderPage() {
    if (!_container) return;

    ensureStyles();

    const visibleItems = _items;

    _container.innerHTML = `
        <div class="ledger-page">
            <div class="ledger-page-header">
                <div>
                    <h2 style="font-size:20px;font-weight:700;color:var(--color-ledger);">💰 账本</h2>
                    <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">记录你的收支明细</p>
                </div>
            </div>

            ${renderSummaryCards(visibleItems)}
            ${renderQuickAdd()}
            ${renderFilterBar()}

            <div class="card">
                <div id="ledger-list">
                    ${renderList(visibleItems)}
                </div>
                <div id="ledger-pagination" class="ledger-pagination"></div>
            </div>
        </div>
    `;

    // Render pagination
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

function attachListeners() {
    if (!_container) return;

    // Quick-add submit
    const qaSubmit = _container.querySelector('#qa-submit');
    if (qaSubmit) {
        qaSubmit.addEventListener('click', handleQuickAdd);
    }

    // Quick-add on Enter in amount/title fields
    ['qa-amount', 'qa-title', 'qa-category'].forEach(id => {
        const el = _container.querySelector(`#${id}`);
        if (el) {
            el.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') handleQuickAdd();
            });
        }
    });

    // Filter selects
    const filterDate = _container.querySelector('#filter-date');
    if (filterDate) {
        filterDate.addEventListener('change', async () => {
            _dateFilter = filterDate.value;
            _page = 1;
            await loadAndRender();
        });
    }
    const filterDir = _container.querySelector('#filter-direction');
    if (filterDir) {
        filterDir.addEventListener('change', async () => {
            _directionFilter = filterDir.value;
            _page = 1;
            await loadAndRender();
        });
    }
    const filterCat = _container.querySelector('#filter-category');
    if (filterCat) {
        filterCat.addEventListener('change', async () => {
            _categoryFilter = filterCat.value;
            _page = 1;
            await loadAndRender();
        });
    }

    // Edit / Delete via event delegation on list
    const list = _container.querySelector('#ledger-list');
    if (list) {
        list.addEventListener('click', async (e) => {
            const editBtn   = e.target.closest('.btn-edit-ledger');
            const deleteBtn = e.target.closest('.btn-delete-ledger');

            if (editBtn) {
                const id   = editBtn.dataset.id;
                const item = _items.find(i => String(i.id) === String(id));
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

    const direction = _container.querySelector('#qa-direction').value;
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
            type:             'ledger',
            direction,
            amount:           parseFloat(amountVal),
            title,
            ledger_category:  category,
            ledger_date:      dateVal || todayStr(),
        });
        showToast('记录已添加', 'success');

        // Reset amount and title fields, keep direction/category/date
        _container.querySelector('#qa-amount').value = '';
        _container.querySelector('#qa-title').value  = '';

        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        _page = 1;
        await loadAndRender();
    } catch (err) {
        showToast('添加失败：' + err.message, 'error');
    }
}

// ── edit modal ────────────────────────────────────────────────────────────────

function openEditModal(existing) {
    const fields = LEDGER_FIELDS.map(f => {
        let value = existing[f.name] !== undefined && existing[f.name] !== null
            ? String(existing[f.name])
            : (f.value !== undefined ? f.value : '');
        return { ...f, value };
    });

    const bodyHTML = `<form id="ledger-edit-form">${buildFormHTML(fields)}</form>`;
    const footer = `
        <button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary" id="modal-cancel">取消</button>
        <button class="btn btn-primary"   id="modal-save">保存</button>
    `;

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

        if (!data.title) {
            showToast('请填写摘要', 'warning');
            return;
        }
        if (!data.amount || data.amount <= 0) {
            showToast('请填写有效金额', 'warning');
            return;
        }

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
    const confirmed = window.confirm('确定要删除这条记录吗？');
    if (!confirmed) return;

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

async function loadAndRender() {
    try {
        const result = await fetchItems(_page);
        _items = result.items;
        _total = result.total;
    } catch (err) {
        _items = [];
        _total = 0;
        showToast('加载账本失败：' + err.message, 'error');
    }
    renderPage();
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    _items     = [];
    _total     = 0;
    _page      = 1;

    // Render skeleton immediately, then load data
    renderPage();
    loadAndRender();

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

export function onRouteEnter(_params) {
    // Nothing special; render() is called by the router
}
