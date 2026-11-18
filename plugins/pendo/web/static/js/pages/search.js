import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { renderPagination } from '../components/pagination.js';
import {
    errorMessage,
    finiteNumber,
    isRecord,
    nonNegativeInteger,
    previewText,
    textValue,
} from '../utils/format.js';
import { formatZonedDateTime } from '../utils/timezone.js';
import { escapeHtml, subscribeDataChanges } from '../utils/ui.js';
import { openEventDetail } from './events.js';
import { openTaskModal } from './tasks.js';
import { openDetailModal as openLedgerDetailModal } from './ledger.js';
import { openNoteViewModal } from './notes.js';
import { openDiaryViewModal } from './diary.js';

const PAGE_SIZE = 20;

const TYPE_CONFIG = Object.freeze({
    event: { label: '日程', module: 'events' },
    task: { label: '待办', module: 'tasks' },
    ledger: { label: '记账', module: 'ledger' },
    note: { label: '笔记', module: 'notes' },
    diary: { label: '日记', module: 'diary' },
});

const TYPE_ORDER        = Object.freeze(['event', 'task', 'ledger', 'note', 'diary']);
const ITEM_TYPES        = new Set(TYPE_ORDER);
const CATEGORY_FIELDS   = new Set(['category', 'ledger_category']);
const TRANSACTION_TYPES = new Set(['expense', 'income', 'transfer']);
const FILTER_TABS       = [
    { value: '', label: '全部' },
    ...TYPE_ORDER.map((type) => ({ value: type, label: TYPE_CONFIG[type].label })),
];
const SUGGESTIONS = [
    { label: '找会议', query: '会议', type: 'event' },
    { label: '找逾期任务', query: '', type: 'task', preset: 'overdue' },
    { label: '找餐饮支出', query: '餐饮', type: 'ledger' },
    { label: '找读书笔记', query: '阅读', type: 'note' },
    { label: '最近 7 天日记', query: '', type: 'diary', preset: 'recent_diary' },
];
const PRESET_LABELS = { overdue: '逾期未完成待办', recent_diary: '最近 7 天日记' };
const TASK_STATUS_LABELS = { open: '待完成', done: '已完成', cancelled: '已取消' };
const MOOD_LABELS = { happy: '开心', sad: '难过', calm: '平静', excited: '兴奋', angry: '生气', tired: '疲惫', anxious: '焦虑', grateful: '感恩', neutral: '普通' };

let _container              = null;
let _query                  = '';
let _preset                 = '';
let _activeType             = '';
let _activeCategory         = '';
let _activeCategoryField    = 'category';
let _activeCategoryTypeHint = '';
let _results                = [];
let _total                  = 0;
let _page                   = 1;
let _loading                = false;
let _hasSearched            = false;
let _unsubscribeDataChanges = null;
let _searchVersion          = 0;
let _lastSearchSignature    = '';

// ---------------------------------------------------------------------------
// 数据边界：未知类型与缺少 ID 的结果不进入页面，其余字段先收敛再渲染
// ---------------------------------------------------------------------------

function normalizeCollection(value) {
    if (!isRecord(value)) return null;
    return {
        ...value,
        title: textValue(value.title).trim(),
        notes: textValue(value.notes),
        location: textValue(value.location).trim(),
        category: textValue(value.category).trim(),
    };
}

function normalizeSearchItem(value) {
    if (!isRecord(value)) return null;
    const id   = textValue(value.id).trim();
    const type = textValue(value.type).trim();
    if (!id || !ITEM_TYPES.has(type)) return null;
    const rawTransactionType = textValue(value.transaction_type).trim();
    const rawAmount = value.amount == null || value.amount === '' ? null : finiteNumber(value.amount, null);
    const rawPriority = value.priority == null || value.priority === '' ? null : finiteNumber(value.priority, null);
    const rawMoodScore =
        value.mood_score == null || value.mood_score === '' ? null : finiteNumber(value.mood_score, null);
    return {
        ...value,
        id,
        type,
        title: textValue(value.title).trim(),
        content: textValue(value.content),
        notes: textValue(value.notes),
        remark: textValue(value.remark),
        location: textValue(value.location).trim(),
        category: textValue(value.category).trim(),
        status: textValue(value.status).trim(),
        priority: rawPriority,
        plan_date: textValue(value.plan_date).trim(),
        deadline_at: textValue(value.deadline_at).trim(),
        start_time: textValue(value.start_time).trim(),
        transaction_type: TRANSACTION_TYPES.has(rawTransactionType) ? rawTransactionType : 'expense',
        amount: rawAmount,
        ledger_category: textValue(value.ledger_category).trim(),
        account_name: textValue(value.account_name).trim(),
        counter_account_name: textValue(value.counter_account_name).trim(),
        merchant: textValue(value.merchant).trim(),
        ledger_date: textValue(value.ledger_date).trim(),
        diary_date: textValue(value.diary_date).trim(),
        entry_time: textValue(value.entry_time).trim(),
        weather: textValue(value.weather).trim(),
        mood: textValue(value.mood).trim(),
        mood_score: rawMoodScore,
        is_favorite: value.is_favorite === true,
        updated_at: textValue(value.updated_at).trim(),
        created_at: textValue(value.created_at).trim(),
        collection: normalizeCollection(value.collection),
    };
}

function normalizeSearchResponse(value) {
    const source = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const items = (Array.isArray(source.items) ? source.items : []).map(normalizeSearchItem).filter(Boolean);
    return {
        items,
        total: Math.max(items.length, nonNegativeInteger(source.total)),
    };
}

function itemTitle(item) {
    // 重复日程的实例与集合同名，只写一次。
    if (item.type === 'event' && item.collection?.title && item.collection.title !== item.title) {
        return `${item.collection.title} · ${item.title || '(无标题)'}`;
    }
    if (item.title) return item.title;
    if (item.type === 'diary' && item.diary_date) return `${item.diary_date} 的日记`;
    return '(无标题)';
}

function itemPreview(item) {
    if (item.type === 'event') {
        return previewText(
            item.notes || item.content || item.collection?.notes || item.collection?.location || item.location || '',
        );
    }
    if (item.type === 'task') return previewText(item.content || '');
    if (item.type === 'ledger') return previewText(item.remark || item.content || '');
    if (item.type === 'diary') return previewText(item.content || '');
    return previewText(item.content || '');
}

function itemMeta(item) {
    if (item.type === 'event') {
        return [
            item.start_time ? formatZonedDateTime(item.start_time, '') : '',
            item.location || item.collection?.location ? item.location || item.collection.location : '',
            item.category || item.collection?.category || '',
        ].filter(Boolean);
    }
    if (item.type === 'task') {
        return [
            TASK_STATUS_LABELS[item.status] || item.status || '',
            item.priority != null ? `优先级 ${item.priority}` : '',
            item.plan_date ? `计划 ${formatZonedDateTime(item.plan_date, '')}` : '',
            item.deadline_at ? `截止 ${formatZonedDateTime(item.deadline_at, '')}` : '',
            item.category || '',
        ].filter(Boolean);
    }
    if (item.type === 'ledger') {
        const txType  = item.transaction_type || 'expense';
        const sign    = txType === 'income' ? '+' : txType === 'transfer' ? '↔ ' : '-';
        const amount  = item.amount != null ? `${sign}${item.currency || 'CNY'} ${item.amount.toFixed(2)}` : '';
        const account = txType === 'transfer' && item.counter_account_name
                ? `${item.account_name || '现金'}→${item.counter_account_name}`
                : item.account_name || '';
        return [amount, item.ledger_category || '', account, item.merchant || '', item.ledger_date || ''].filter(
            Boolean,
        );
    }
    if (item.type === 'diary') {
        const entryTime = item.entry_time ? formatZonedDateTime(item.entry_time, '') : '';
        return [
            entryTime || item.diary_date || '',
            item.weather || '',
            item.mood ? `心情 ${MOOD_LABELS[item.mood] || item.mood}` : '',
            item.mood_score != null ? `${item.mood_score}/10` : '',
            item.is_favorite ? '收藏' : '',
        ].filter(Boolean);
    }
    return [
        item.category || '',
        item.updated_at || item.created_at
            ? formatZonedDateTime(item.updated_at || item.created_at, '')
            : '',
    ].filter(Boolean);
}

function resultCounts() {
    const counts = Object.fromEntries(TYPE_ORDER.map((type) => [type, 0]));
    _results.forEach((item) => {
        counts[item.type] = (counts[item.type] || 0) + 1;
    });
    return counts;
}

function totalPages() {
    return Math.max(1, Math.ceil(nonNegativeInteger(_total) / PAGE_SIZE));
}

function matchingCategories() {
    const map = new Map();
    _results.forEach((item) => {
        const field    = item.type === 'ledger' ? 'ledger_category' : 'category';
        const category = field === 'ledger_category' ? item.ledger_category : item.category;
        if (!category) return;
        const key   = `${field}:${category}`;
        const entry = map.get(key) || { category, count: 0, field, typeHint: item.type };
        entry.count += 1;
        if (entry.typeHint !== item.type) entry.typeHint = '';
        map.set(key, entry);
    });
    return [...map.values()].sort((a, b) => b.count - a.count || a.category.localeCompare(b.category)).slice(0, 6);
}

function groupedResults() {
    if (_activeType) return [{ type: _activeType, items: _results }];
    return TYPE_ORDER.map((type) => ({ type, items: _results.filter((item) => item.type === type) })).filter(
        (group) => group.items.length,
    );
}

function renderTabs() {
    return `<div class="search-filter-tabs" role="group" aria-label="结果类型">${FILTER_TABS.map((tab) => {
        const active = _activeType === tab.value;
        return `<button type="button" class="search-tab${active ? ' active' : ''}"
                        data-type="${escapeHtml(tab.value)}" aria-pressed="${active}">${escapeHtml(tab.label)}</button>`;
    }).join('')}</div>`;
}

function renderSuggestions() {
    return `<div class="search-chip-row" aria-label="搜索建议">${SUGGESTIONS.map(
        (item) => `
        <button type="button" class="search-chip search-suggestion"
                data-suggest-query="${escapeHtml(item.query)}" data-suggest-type="${escapeHtml(item.type)}" data-suggest-preset="${escapeHtml(item.preset || '')}">
            ${escapeHtml(item.label)}
        </button>
    `,
    ).join('')}</div>`;
}

function renderCategoryChips() {
    const categories = matchingCategories();
    if (!categories.length || !_hasSearched) return '';
    return `<div class="search-chip-row" role="group" aria-label="本页主题">${categories
        .map((item) => {
            const active = _activeCategory === item.category && _activeCategoryField === item.field;
            return `
            <button type="button" class="search-chip${active ? ' active' : ''}"
                    data-category="${escapeHtml(item.category)}"
                    data-category-field="${escapeHtml(item.field)}"
                    data-category-type="${escapeHtml(item.typeHint || '')}"
                    aria-pressed="${active}">${escapeHtml(item.category)} · ${item.count}</button>
        `;
        })
        .join('')}</div>`;
}

function renderSummary() {
    if ((!_query.trim() && !_preset) || !_hasSearched) return '';
    const counts       = resultCounts();
    const activeTypes  = TYPE_ORDER.filter((type) => counts[type]);
    const visibleCount = _results.length;
    const total        = nonNegativeInteger(_total);
    const summary      =         total > visibleCount ? `当前共命中 ${total} 条，本页展示 ${visibleCount} 条` : `当前共命中 ${total} 条`;
    return `
        <section class="search-summary">
            <div>
                <h3>检索结果</h3>
                <p>“${escapeHtml(PRESET_LABELS[_preset] || _query)}”${summary}，本页覆盖 ${activeTypes.length || 0} 个模块。</p>
            </div>
            <div class="search-query-meta">
                ${_total > PAGE_SIZE ? `<span class="search-pill">第 ${_page} / ${totalPages()} 页</span>` : ''}
                ${activeTypes.map((type) => `<span class="search-pill"><span class="pixel-icon" data-module="${TYPE_CONFIG[type].module}" aria-hidden="true"></span>${TYPE_CONFIG[type].label} · ${counts[type]}</span>`).join('')}
            </div>
        </section>
    `;
}

function renderCard(item) {
    const cfg      = TYPE_CONFIG[item.type];
    const title    = itemTitle(item);
    const preview  = itemPreview(item);
    const metadata = itemMeta(item);
    return `
        <button class="search-card search-type-${item.type}" type="button"
                data-open-result="${escapeHtml(item.id)}" aria-label="${escapeHtml(`打开${cfg.label}：${title}`)}">
            <span class="search-card-icon pixel-icon" data-module="${cfg.module}" aria-hidden="true"></span>
            <span class="search-card-body">
                <span class="search-card-title-row">
                    <span class="search-card-title">${escapeHtml(title)}</span>
                    <span class="search-card-badge">${cfg.label}</span>
                </span>
                ${preview ? `<span class="search-card-preview">${escapeHtml(preview)}</span>` : ''}
                <span class="search-card-meta">${metadata.map((meta) => `<span>${escapeHtml(meta)}</span>`).join('')}</span>
            </span>
            <span class="search-card-arrow" aria-hidden="true">→</span>
        </button>
    `;
}

function renderResults() {
    if (_loading)
        return '<div class="search-empty" role="status"><span class="search-empty-icon pixel-icon" data-icon="hourglass" aria-hidden="true"></span><div>正在搜索...</div></div>';
    if (!_query.trim() && !_preset) {
        return `<div class="search-empty"><span class="search-empty-icon pixel-icon" data-icon="search" aria-hidden="true"></span><div>输入关键词开始检索，也可以直接点上面的建议。</div></div>`;
    }
    if (_hasSearched && !_results.length) {
        return `<div class="search-empty"><span class="search-empty-icon pixel-icon" data-icon="compass" aria-hidden="true"></span><div>没有找到结果。试试换关键词、切类型，或先清空主题切片。</div></div>`;
    }
    return `<div class="search-results-stack">${groupedResults()
        .map(
            (group) => `
        <section class="search-group">
            <div class="search-group-head">
                <div class="search-group-title"><span class="pixel-icon" data-module="${TYPE_CONFIG[group.type].module}" aria-hidden="true"></span>${TYPE_CONFIG[group.type].label}</div>
                <span class="search-group-count">${group.items.length}</span>
            </div>
            <div class="search-card-list">${group.items.map(renderCard).join('')}</div>
        </section>`,
        )
        .join('')}</div>`;
}

function renderPage() {
    if (!_container) return;
    const typeCounts  = resultCounts();
    const activeTypes = TYPE_ORDER.filter((type) => typeCounts[type]);
    _container.innerHTML = `
        <div class="search-shell">
            <section class="search-hero pendo-page-header">
                <div>
                    <span class="eyebrow"><i></i>SEARCH</span>
                    <h2>搜索</h2>
                    <p>在日程、待办、记账、笔记和日记中统一搜索。</p>
                    <div class="search-hero-tags">
                        <span class="search-hero-tag">${_activeType ? TYPE_CONFIG[_activeType].label : '全部类型'}</span>
                        <span class="search-hero-tag">${escapeHtml(_activeCategory || '全部主题')}</span>
                        <span class="search-hero-tag">${_hasSearched ? `${nonNegativeInteger(_total)} 条命中` : '等待检索'}</span>
                    </div>
                </div>
                <div class="search-query-meta">
                    ${activeTypes
                        .slice(0, 3)
                        .map(
                            (type) => `<span class="search-pill">${TYPE_CONFIG[type].label} ${typeCounts[type]}</span>`,
                        )
                        .join('')}
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
                    <input id="search-input" class="search-input" type="text" autocomplete="off" placeholder="搜索标题、内容、标签、地点、备注、天气..." value="${escapeHtml(_query)}" aria-label="全局搜索关键词">
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
                const nextPage = Math.max(1, nonNegativeInteger(page));
                if (nextPage === _page) return;
                _page = nextPage;
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

// ---------------------------------------------------------------------------
// 详情与查询：详情按声明类型分发，查询采用“后发请求胜出”的版本门禁
// ---------------------------------------------------------------------------

async function openResultDetail(item) {
    const normalizedItem = normalizeSearchItem(item);
    if (!normalizedItem) return;
    if (normalizedItem.type === 'event') {
        await openEventDetail(normalizedItem.id);
        return;
    }
    try {
        const res       = await api.get(`/items/${encodeURIComponent(normalizedItem.id)}`);
        const rawLatest =             res?.data && typeof res.data === 'object' && !Array.isArray(res.data) ? res.data : normalizedItem;
        const latest =
            normalizeSearchItem({
                ...rawLatest,
                id: normalizedItem.id,
                type: normalizedItem.type,
            }) || normalizedItem;
        if (normalizedItem.type === 'task') {
            openTaskModal(latest);
            return;
        }
        if (normalizedItem.type === 'ledger') {
            openLedgerDetailModal(latest);
            return;
        }
        if (normalizedItem.type === 'note') {
            openNoteViewModal(latest);
            return;
        }
        if (normalizedItem.type === 'diary') {
            openDiaryViewModal(latest);
            return;
        }
        showToast('暂不支持打开这种搜索结果', 'warning');
    } catch (error) {
        showToast(`加载详情失败：${errorMessage(error)}`, 'error');
    }
}

function searchParams(query, page) {
    const params = { q: query, page, page_size: PAGE_SIZE };
    if (_preset) params.preset = _preset;
    if (_activeType) params.type = _activeType;
    if (_activeCategoryField === 'ledger_category' && _activeCategory) {
        params.ledger_category = _activeCategory;
        if (!_activeType && _activeCategoryTypeHint === 'ledger') params.type = 'ledger';
    } else if (_activeCategory) {
        params.category = _activeCategory;
    }
    return params;
}

function searchSignature(query, page) {
    return JSON.stringify([query, page, _activeType, _activeCategory, _activeCategoryField, _activeCategoryTypeHint, _preset]);
}

async function doSearch({ resetPage = false, force = false } = {}) {
    if (resetPage) _page = 1;
    const q = _query.trim();
    if (!q && !_preset) {
        _searchVersion += 1;
        _query = '';
        _results = [];
        _total = 0;
        _page = 1;
        _loading = false;
        _hasSearched = false;
        _lastSearchSignature = '';
        renderPage();
        return;
    }
    const root = _container;
    if (!root) return;
    const requestedPage = Math.max(1, nonNegativeInteger(_page));
    const signature     = searchSignature(q, requestedPage);
    if (!force && signature === _lastSearchSignature && (_loading || _hasSearched)) return;

    const version = ++_searchVersion;
    _query = q;
    _page = requestedPage;
    _lastSearchSignature = signature;
    _loading = true;
    renderPage();
    try {
        let response = normalizeSearchResponse((await api.get('/search', searchParams(q, requestedPage)))?.data);
        if (_container !== root || version !== _searchVersion) return;

        const lastPage = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
        if (requestedPage > lastPage) {
            _page = lastPage;
            response = normalizeSearchResponse((await api.get('/search', searchParams(q, lastPage)))?.data);
            if (_container !== root || version !== _searchVersion) return;
            _lastSearchSignature = searchSignature(q, lastPage);
        }
        _results = response.items;
        _total = response.total;
        _hasSearched = true;
    } catch (error) {
        if (_container !== root || version !== _searchVersion) return;
        _results = [];
        _total = 0;
        _hasSearched = true;
        _lastSearchSignature = '';
        showToast(`搜索失败：${errorMessage(error)}`, 'error');
    } finally {
        if (_container === root && version === _searchVersion) {
            _loading = false;
            renderPage();
        }
    }
}

// 每次重绘都会替换内部节点，因此监听器只绑定到本轮 DOM，避免累积。
function attachListeners() {
    const root = _container;
    if (!root) return;
    const input = root.querySelector('#search-input');
    if (input) {
        input.oninput = () => {
            _preset = '';
            _query = input.value;
        };
        input.onchange = async () => {
            _query = input.value;
            await doSearch({ resetPage: true });
        };
        input.onkeydown = async (event) => {
            if (event.key === 'Enter' && !event.isComposing) {
                event.preventDefault();
                _query = input.value;
                await doSearch({ resetPage: true });
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                _query = '';
                _preset = '';
                _activeCategory = '';
                _activeCategoryField = 'category';
                _activeCategoryTypeHint = '';
                await doSearch({ resetPage: true });
            }
        };
    }

    root.querySelectorAll('.search-tab[data-type]').forEach((button) => {
        button.onclick = async () => {
            const requestedType = textValue(button.dataset.type).trim();
            const nextType      = ITEM_TYPES.has(requestedType) ? requestedType : '';
            if (nextType === _activeType && !_activeCategory) return;
            _activeType = nextType;
            _preset = '';
            _activeCategory = '';
            _activeCategoryField = 'category';
            _activeCategoryTypeHint = '';
            if (_query.trim() || _preset) await doSearch({ resetPage: true });
            else renderPage();
        };
    });

    root.querySelectorAll('.search-chip[data-category]').forEach((button) => {
        button.onclick = async () => {
            const category = textValue(button.dataset.category).trim();
            const requestedField = textValue(button.dataset.categoryField).trim();
            const field = CATEGORY_FIELDS.has(requestedField) ? requestedField : 'category';
            const requestedHint = textValue(button.dataset.categoryType).trim();
            const typeHint = ITEM_TYPES.has(requestedHint) ? requestedHint : '';
            const isSame = _activeCategory === category && _activeCategoryField === field;
            _activeCategory = isSame ? '' : category;
            _activeCategoryField = isSame ? 'category' : field;
            _activeCategoryTypeHint = isSame ? '' : typeHint;
            if (_query.trim() || _preset) await doSearch({ resetPage: true });
            else renderPage();
        };
    });

    root.querySelectorAll('.search-suggestion[data-suggest-query]').forEach((button) => {
        button.onclick = async () => {
            _query = textValue(button.dataset.suggestQuery).trim();
            _preset = PRESET_LABELS[button.dataset.suggestPreset] ? button.dataset.suggestPreset : '';
            const requestedType = textValue(button.dataset.suggestType).trim();
            _activeType = ITEM_TYPES.has(requestedType) ? requestedType : '';
            _activeCategory = '';
            _activeCategoryField = 'category';
            _activeCategoryTypeHint = '';
            await doSearch({ resetPage: true });
        };
    });

    root.querySelectorAll('[data-open-result]').forEach((card) => {
        card.onclick = async () => {
            const id   = textValue(card.dataset.openResult);
            const item = _results.find((entry) => entry.id === id);
            if (item) await openResultDetail(item);
        };
    });
}

export async function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('搜索页需要有效的容器元素');
    }
    _searchVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
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
    _lastSearchSignature = '';
    _unsubscribeDataChanges = subscribeDataChanges(null, async () => {
        if ((!_query.trim() && !_preset) || !_hasSearched) return;
        await doSearch({ force: true });
    });
    if (_query.trim() || _preset) await doSearch({ resetPage: true, force: true });
    else renderPage();
}

export function destroy() {
    _searchVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
    _results = [];
    _total = 0;
    _page = 1;
    _query = '';
    _preset = '';
    _activeType = '';
    _activeCategory = '';
    _activeCategoryField = 'category';
    _activeCategoryTypeHint = '';
    _loading = false;
    _hasSearched = false;
    _lastSearchSignature = '';
}

export function onRouteEnter(params) {
    const q = params && typeof params.get === 'function' ? params.get('q') : '';
    _query = textValue(q).trim();
    _preset = '';
}
