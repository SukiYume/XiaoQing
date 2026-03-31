import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { renderPagination } from '../components/pagination.js';
import { formatDateTime, previewText } from '../utils/format.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';
import { openEventDetail } from './events.js';
import { openTaskModal } from './tasks.js';
import { openDetailModal as openLedgerDetailModal } from './ledger.js';
import { openNoteViewModal } from './notes.js';
import { openDiaryViewModal } from './diary.js';

const CSS_ID = 'pendo-search-redesign-styles';
const PAGE_SIZE = 20;

const TYPE_CONFIG = {
    event: { label: '日程', icon: '📅', color: '#F59E0B' },
    task: { label: '待办', icon: '✅', color: '#10B981' },
    ledger: { label: '记账', icon: '💰', color: '#EF4444' },
    note: { label: '笔记', icon: '📝', color: '#3B82F6' },
    diary: { label: '日记', icon: '📔', color: '#EC4899' },
};

const TYPE_ORDER = ['event', 'task', 'ledger', 'note', 'diary'];
const FILTER_TABS = [{ value: '', label: '全部' }, ...TYPE_ORDER.map((type) => ({ value: type, label: TYPE_CONFIG[type].label }))];
const SUGGESTIONS = [
    { label: '找会议', query: '会议', type: 'event' },
    { label: '找逾期任务', query: '截止', type: 'task' },
    { label: '找餐饮支出', query: '餐饮', type: 'ledger' },
    { label: '找读书笔记', query: '阅读', type: 'note' },
    { label: '找最近日记', query: '今天', type: 'diary' },
];

let _container = null;
let _query = '';
let _activeType = '';
let _activeCategory = '';
let _activeCategoryField = 'category';
let _activeCategoryTypeHint = '';
let _results = [];
let _total = 0;
let _page = 1;
let _loading = false;
let _hasSearched = false;
let _debounceTimer = null;
let _dataChangedHandler = null;

function formatDate(value) {
    return value ? formatDateTime(value, '') : '';
}

function alphaColor(hex, alpha = 0.12) {
    const value = String(hex || '').trim();
    const normalized = value.startsWith('#') ? value.slice(1) : value;
    if (![3, 6].includes(normalized.length) || /[^0-9a-f]/i.test(normalized)) {
        return `rgba(148,163,184,${alpha})`;
    }
    const full = normalized.length === 3
        ? normalized.split('').map((part) => `${part}${part}`).join('')
        : normalized;
    const int = Number.parseInt(full, 16);
    const r = (int >> 16) & 255;
    const g = (int >> 8) & 255;
    const b = int & 255;
    return `rgba(${r},${g},${b},${alpha})`;
}

function itemTitle(item) {
    if (item.title) return item.title;
    if (item.type === 'diary' && item.diary_date) return `${item.diary_date} 的日记`;
    return '(无标题)';
}

function itemPreview(item) {
    if (item.type === 'event') return previewText(item.notes || item.content || item.location || '');
    if (item.type === 'task') return previewText(item.content || '');
    if (item.type === 'ledger') return previewText(item.remark || item.content || '');
    if (item.type === 'diary') return previewText(item.content || '');
    return previewText(item.content || '');
}

function itemMeta(item) {
    if (item.type === 'event') {
        return [item.start_time ? formatDate(item.start_time) : '', item.location ? `📍 ${item.location}` : '', item.category || ''].filter(Boolean);
    }
    if (item.type === 'task') {
        return [item.status || '', item.priority ? `优先级 ${item.priority}` : '', item.due_time ? `截止 ${formatDate(item.due_time)}` : '', item.category || ''].filter(Boolean);
    }
    if (item.type === 'ledger') {
        const amount = item.amount != null ? `${item.direction === 'income' ? '+' : '-'}¥${Number(item.amount).toFixed(2)}` : '';
        return [amount, item.ledger_category || '', item.ledger_date || ''].filter(Boolean);
    }
    if (item.type === 'diary') {
        return [item.diary_date || '', item.weather || '', item.mood || ''].filter(Boolean);
    }
    return [item.category || '', formatDate(item.updated_at || item.created_at)].filter(Boolean);
}

function resultCounts() {
    const counts = {};
    _results.forEach((item) => {
        counts[item.type] = (counts[item.type] || 0) + 1;
    });
    return counts;
}

function totalPages() {
    return Math.max(1, Math.ceil((_total || 0) / PAGE_SIZE));
}

function matchingCategories() {
    const map = new Map();
    _results.forEach((item) => {
        const field = item.type === 'ledger' ? 'ledger_category' : 'category';
        const category = field === 'ledger_category' ? item.ledger_category : item.category;
        if (!category) return;
        const key = `${field}:${category}`;
        const entry = map.get(key) || { category, count: 0, field, typeHint: item.type };
        entry.count += 1;
        if (entry.typeHint !== item.type) entry.typeHint = '';
        map.set(key, entry);
    });
    return [...map.values()]
        .sort((a, b) => b.count - a.count || a.category.localeCompare(b.category))
        .slice(0, 6);
}

function groupedResults() {
    if (_activeType) return [{ type: _activeType, items: _results }];
    return TYPE_ORDER
        .map((type) => ({ type, items: _results.filter((item) => item.type === type) }))
        .filter((group) => group.items.length);
}

function ensureStyles() {
    injectStyles(CSS_ID, `
        ${pageShellCss('search-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .search-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
            padding: 24px 26px; border-radius: 28px; margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(100,116,139,0.18), transparent 32%),
                radial-gradient(circle at bottom left, rgba(148,163,184,0.12), transparent 22%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(148,163,184,0.16); box-shadow: 0 18px 40px rgba(15,23,42,0.05);
        }
        .search-hero h2 {
            margin: 0; display: inline-flex; align-items: center; gap: 10px;
            font-size: 30px; font-weight: 820; letter-spacing: -0.03em; color: #334155;
        }
        .search-hero-icon {
            display: inline-flex; align-items: center; justify-content: center;
            width: 40px; height: 40px; border-radius: 14px; flex: 0 0 auto;
            background: linear-gradient(135deg, rgba(99,102,241,0.12), rgba(59,130,246,0.18));
            font-size: 24px; line-height: 1;
        }
        .search-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .search-hero-tags { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
        .search-hero-tag {
            display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 999px;
            background: rgba(148,163,184,0.10); color: #475569; font-size: 12px; font-weight: 700;
        }
        .search-query-bar {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center;
            padding: 16px 18px; margin-bottom: 16px; border-radius: 24px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(203,213,225,0.9); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .search-input-wrap { position: relative; }
        .search-input-wrap .search-input {
            width: 100%; height: 46px; box-sizing: border-box; padding: 0 16px 0 50px;
            border-radius: 16px; border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.92);
            font-size: 15px; color: var(--color-text);
        }
        .search-input-wrap .search-input:focus { outline: none; border-color: #64748B; box-shadow: 0 0 0 3px rgba(148,163,184,0.16); }
        .search-input-icon {
            position: absolute; left: 18px; top: 50%; transform: translateY(-50%);
            width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center;
            color: var(--color-text-tertiary); pointer-events: none;
        }
        .search-input-icon svg { width: 18px; height: 18px; display: block; }
        .search-query-meta { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        .search-pill {
            display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: 999px;
            background: rgba(255,255,255,0.9); border: 1px solid rgba(203,213,225,0.9); color: #475569; font-size: 12px; font-weight: 700;
        }
        .search-filter-tabs, .search-chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
        .search-tab, .search-chip {
            border: 1px solid rgba(203,213,225,0.9); background: rgba(255,255,255,0.9); color: #475569; cursor: pointer;
            border-radius: 999px; padding: 9px 14px; font-size: 12px; font-weight: 700;
        }
        .search-tab.active, .search-chip.active { background: #475569; color: #fff; border-color: #475569; }
        .search-chip.search-suggestion { background: rgba(241,245,249,0.9); }
        .search-summary {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: start;
            padding: 16px 18px; margin-bottom: 16px; border-radius: 24px; background: rgba(255,255,255,0.92);
            border: 1px solid rgba(203,213,225,0.85);
        }
        .search-summary h3 { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); }
        .search-summary p { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .search-results-stack { display: flex; flex-direction: column; gap: 16px; }
        .search-group {
            padding: 18px; border-radius: 24px; background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(203,213,225,0.88); box-shadow: 0 16px 34px rgba(15,23,42,0.04);
        }
        .search-group-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
        .search-group-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 780; color: var(--color-text); }
        .search-group-count { min-width: 28px; height: 28px; padding: 0 10px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; background: rgba(226,232,240,0.9); color: #475569; font-size: 12px; font-weight: 800; }
        .search-card-list { display: flex; flex-direction: column; gap: 12px; }
        .search-card {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: start;
            padding: 14px; border-radius: 20px; border: 1px solid rgba(203,213,225,0.82); background: rgba(255,255,255,0.96);
            cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
        }
        .search-card:hover { transform: translateY(-1px); border-color: rgba(100,116,139,0.28); box-shadow: 0 14px 28px rgba(15,23,42,0.06); }
        .search-card-icon {
            width: 42px; height: 42px; border-radius: 14px; display: inline-flex; align-items: center; justify-content: center;
            font-size: 18px;
        }
        .search-card-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .search-card-title { margin: 0; font-size: 15px; font-weight: 780; color: var(--color-text); }
        .search-card-badge { padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; color: #fff; }
        .search-card-preview { margin-top: 6px; font-size: 13px; line-height: 1.65; color: var(--color-text-secondary); }
        .search-card-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
        .search-card-meta span {
            display: inline-flex; align-items: center; height: 28px; padding: 0 10px; border-radius: 999px;
            background: rgba(241,245,249,0.92); color: #475569; font-size: 12px; font-weight: 700;
        }
        .search-card-arrow { color: var(--color-text-tertiary); font-size: 16px; padding-top: 8px; }
        .search-empty {
            padding: 48px 18px; border-radius: 24px; text-align: center; background: rgba(255,255,255,0.92);
            border: 1px dashed rgba(148,163,184,0.28); color: var(--color-text-secondary);
        }
        .search-empty-icon { font-size: 40px; margin-bottom: 10px; }
        ${mediaMax(BREAKPOINTS.MOBILE, `
            .search-hero, .search-query-bar, .search-summary { grid-template-columns: 1fr; }
            .search-query-meta { justify-content: flex-start; }
            .search-card { grid-template-columns: auto minmax(0, 1fr); }
            .search-card-arrow { display: none; }
        `)}
    `);
}

function renderTabs() {
    return `<div class="search-filter-tabs">${FILTER_TABS.map((tab) => {
        const active = _activeType === tab.value;
        return `<button type="button" class="search-tab${active ? ' active' : ''}" data-type="${tab.value}">${tab.label}</button>`;
    }).join('')}</div>`;
}

function renderSuggestions() {
    return `<div class="search-chip-row">${SUGGESTIONS.map((item) => `<button type="button" class="search-chip search-suggestion" data-suggest-query="${item.query}" data-suggest-type="${item.type}">${item.label}</button>`).join('')}</div>`;
}

function renderCategoryChips() {
    const categories = matchingCategories();
    if (!categories.length || !_hasSearched) return '';
    return `<div class="search-chip-row">${categories.map((item) => `<button type="button" class="search-chip${_activeCategory === item.category && _activeCategoryField === item.field ? ' active' : ''}" data-category="${escapeHtml(item.category)}" data-category-field="${item.field}" data-category-type="${item.typeHint || ''}">${escapeHtml(item.category)} · ${item.count}</button>`).join('')}</div>`;
}

function renderSummary() {
    if (!_query.trim() || !_hasSearched) return '';
    const counts = resultCounts();
    const activeTypes = TYPE_ORDER.filter((type) => counts[type]);
    const visibleCount = _results.length;
    const summary = _total > visibleCount
        ? `当前共命中 ${_total} 条，本页展示 ${visibleCount} 条`
        : `当前共命中 ${_total} 条`;
    return `
        <section class="search-summary">
            <div>
                <h3>检索结果</h3>
                <p>“${escapeHtml(_query)}”${summary}，本页覆盖 ${activeTypes.length || 0} 个模块。</p>
            </div>
            <div class="search-query-meta">
                ${_total > PAGE_SIZE ? `<span class="search-pill">第 ${_page} / ${totalPages()} 页</span>` : ''}
                ${activeTypes.map((type) => `<span class="search-pill">${TYPE_CONFIG[type].icon} ${TYPE_CONFIG[type].label} · ${counts[type]}</span>`).join('')}
            </div>
        </section>
    `;
}

function renderCard(item) {
    const cfg = TYPE_CONFIG[item.type] || { label: item.type, icon: '❓', color: '#94A3B8' };
    const iconBg = alphaColor(cfg.color, 0.14);
    return `
        <article class="search-card" data-id="${escapeHtml(String(item.id))}">
            <span class="search-card-icon" style="background:${iconBg};color:${cfg.color};">${cfg.icon}</span>
            <div>
                <div class="search-card-title-row">
                    <h4 class="search-card-title">${escapeHtml(itemTitle(item))}</h4>
                    <span class="search-card-badge" style="background:${cfg.color};">${cfg.label}</span>
                </div>
                ${itemPreview(item) ? `<div class="search-card-preview">${escapeHtml(itemPreview(item))}</div>` : ''}
                <div class="search-card-meta">${itemMeta(item).map((meta) => `<span>${escapeHtml(meta)}</span>`).join('')}</div>
            </div>
            <span class="search-card-arrow">→</span>
        </article>
    `;
}

function renderResults() {
    if (_loading) return '<div class="search-empty"><div class="search-empty-icon">⏳</div><div>正在搜索...</div></div>';
    if (!_query.trim()) {
        return `<div class="search-empty"><div class="search-empty-icon">🔎</div><div>输入关键词开始检索，下面的建议可以直接点。</div></div>`;
    }
    if (_hasSearched && !_results.length) {
        return `<div class="search-empty"><div class="search-empty-icon">🧭</div><div>没有找到结果。试试换关键词、切类型，或先清空主题切片。</div></div>`;
    }
    return `<div class="search-results-stack">${groupedResults().map((group) => `
        <section class="search-group">
            <div class="search-group-head">
                <div class="search-group-title">${TYPE_CONFIG[group.type].icon} ${TYPE_CONFIG[group.type].label}</div>
                <span class="search-group-count">${group.items.length}</span>
            </div>
            <div class="search-card-list">${group.items.map(renderCard).join('')}</div>
        </section>`).join('')}</div>`;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    const typeCounts = resultCounts();
    const activeTypes = TYPE_ORDER.filter((type) => typeCounts[type]);
    _container.innerHTML = `
        <div class="search-shell">
            <section class="search-hero">
                <div>
                    <h2><span class="search-hero-icon" aria-hidden="true">🔍</span><span>搜索</span></h2>
                    <p>在日程、待办、记账、笔记和日记中统一搜索。</p>
                    <div class="search-hero-tags">
                        <span class="search-hero-tag">${_activeType ? TYPE_CONFIG[_activeType].label : '全部类型'}</span>
                        <span class="search-hero-tag">${_activeCategory || '全部主题'}</span>
                        <span class="search-hero-tag">${_hasSearched ? `${_total} 条命中` : '等待检索'}</span>
                    </div>
                </div>
                <div class="search-query-meta">
                    ${activeTypes.slice(0, 3).map((type) => `<span class="search-pill">${TYPE_CONFIG[type].label} ${typeCounts[type]}</span>`).join('')}
                </div>
            </section>

            <section class="search-query-bar">
                <div class="search-input-wrap">
                    <span class="search-input-icon" aria-hidden="true">
                        <svg viewBox="0 0 20 20" fill="none" focusable="false">
                            <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" stroke-width="1.8"></circle>
                            <path d="M12.5 12.5L17 17" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"></path>
                        </svg>
                    </span>
                    <input id="search-input" class="search-input" type="text" autocomplete="off" placeholder="搜索标题、内容、标签、地点、备注、天气..." value="${escapeHtml(_query)}">
                </div>
                <div class="search-query-meta">
                    <span class="search-pill">Enter 立即搜索</span>
                    <span class="search-pill">Esc 清空</span>
                </div>
            </section>

            ${renderTabs()}
            ${renderSuggestions()}
            ${renderCategoryChips()}
            ${renderSummary()}
            <div id="search-results-area">${renderResults()}</div>
            <div id="search-pagination" class="search-pagination"></div>
        </div>
    `;
    const paginationEl = _container.querySelector('#search-pagination');
    if (paginationEl) {
        renderPagination(paginationEl, {
            page: _page,
            pageSize: PAGE_SIZE,
            total: _total,
            onChange: async (page) => {
                _page = page;
                await doSearch();
            },
        });
    }
    attachListeners();
    const input = _container.querySelector('#search-input');
    if (input) {
        input.focus();
        const length = input.value.length;
        input.setSelectionRange(length, length);
    }
}

async function openResultDetail(item) {
    if (!item?.id || !item?.type) return;
    if (item.type === 'event') {
        await openEventDetail(item.id);
        return;
    }
    try {
        const res = await api.get(`/items/${item.id}`);
        const latest = res?.data || item;
        if (item.type === 'task') {
            openTaskModal(latest);
            return;
        }
        if (item.type === 'ledger') {
            openLedgerDetailModal(latest);
            return;
        }
        if (item.type === 'note') {
            openNoteViewModal(latest);
            return;
        }
        if (item.type === 'diary') {
            openDiaryViewModal(latest);
            return;
        }
        showToast('暂不支持打开这种搜索结果', 'warning');
    } catch (err) {
        showToast(`加载详情失败：${err.message}`, 'error');
    }
}

async function doSearch(options = {}) {
    const { resetPage = false } = options;
    if (resetPage) _page = 1;
    const q = _query.trim();
    if (!q) {
        _results = [];
        _total = 0;
        _page = 1;
        _loading = false;
        _hasSearched = false;
        renderPage();
        return;
    }
    _loading = true;
    renderPage();
    try {
        const params = { q, page: _page, page_size: PAGE_SIZE };
        if (_activeType) params.type = _activeType;
        if (_activeCategoryField === 'ledger_category' && _activeCategory) {
            params.ledger_category = _activeCategory;
            if (!_activeType && _activeCategoryTypeHint === 'ledger') params.type = 'ledger';
        } else if (_activeCategory) {
            params.category = _activeCategory;
        }
        const res = await api.get('/search', params);
        _results = res?.data?.items || [];
        _total = Number(res?.data?.total || 0);
        _hasSearched = true;
        const lastPage = totalPages();
        if (_page > lastPage) {
            _page = lastPage;
            await doSearch();
            return;
        }
    } catch (err) {
        _results = [];
        _total = 0;
        _hasSearched = true;
        showToast(`搜索失败：${err.message}`, 'error');
    }
    _loading = false;
    renderPage();
}

function attachListeners() {
    if (!_container) return;
    const input = _container.querySelector('#search-input');
    if (input) {
        input.oninput = () => {
            _query = input.value;
        };
        input.onchange = async () => {
            _query = input.value;
            clearTimeout(_debounceTimer);
            if (!_query.trim()) {
                _results = [];
                _total = 0;
                _page = 1;
                _hasSearched = false;
                _loading = false;
                renderPage();
                return;
            }
            await doSearch({ resetPage: true });
        };
        input.onkeydown = async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                clearTimeout(_debounceTimer);
                _query = input.value;
                await doSearch({ resetPage: true });
            }
            if (event.key === 'Escape') {
                clearTimeout(_debounceTimer);
                _query = '';
                _activeCategory = '';
                _activeCategoryField = 'category';
                _activeCategoryTypeHint = '';
                _results = [];
                _total = 0;
                _page = 1;
                _hasSearched = false;
                _loading = false;
                renderPage();
            }
        };
    }

    _container.querySelectorAll('.search-tab[data-type]').forEach((button) => {
        button.onclick = async () => {
            _activeType = button.dataset.type || '';
            _activeCategory = '';
            _activeCategoryField = 'category';
            _activeCategoryTypeHint = '';
            if (_query.trim()) await doSearch({ resetPage: true });
            else renderPage();
        };
    });

    _container.querySelectorAll('.search-chip[data-category]').forEach((button) => {
        button.onclick = async () => {
            const category = button.dataset.category || '';
            const field = button.dataset.categoryField || 'category';
            const typeHint = button.dataset.categoryType || '';
            const isSame = _activeCategory === category && _activeCategoryField === field;
            _activeCategory = isSame ? '' : category;
            _activeCategoryField = isSame ? 'category' : field;
            _activeCategoryTypeHint = isSame ? '' : typeHint;
            if (_query.trim()) await doSearch({ resetPage: true });
            else renderPage();
        };
    });

    _container.querySelectorAll('.search-suggestion[data-suggest-query]').forEach((button) => {
        button.onclick = async () => {
            _query = button.dataset.suggestQuery || '';
            _activeType = button.dataset.suggestType || '';
            _activeCategory = '';
            _activeCategoryField = 'category';
            _activeCategoryTypeHint = '';
            await doSearch({ resetPage: true });
        };
    });

    _container.querySelectorAll('.search-card[data-id]').forEach((card) => {
        card.onclick = async () => {
            const item = _results.find((entry) => String(entry.id) === String(card.dataset.id));
            if (item) await openResultDetail(item);
        };
    });
}

export function render(container) {
    _container = container;
    _results = [];
    _total = 0;
    _page = 1;
    _activeType = '';
    _activeCategory = '';
    _activeCategoryField = 'category';
    _activeCategoryTypeHint = '';
    _loading = false;
    _hasSearched = false;
    renderPage();
    _dataChangedHandler = async () => {
        if (!_query.trim() || !_hasSearched) return;
        await doSearch();
    };
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    clearTimeout(_debounceTimer);
    _debounceTimer = null;
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
    _results = [];
    _total = 0;
    _page = 1;
    _activeCategory = '';
    _activeCategoryField = 'category';
    _activeCategoryTypeHint = '';
    _loading = false;
    _hasSearched = false;
}

export function onRouteEnter(params) {
    const q = params ? params.get('q') : '';
    if (!q) return;
    _query = q;
    _page = 1;
    doSearch();
}
