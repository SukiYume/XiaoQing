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
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, subscribeDataChanges } from '../utils/ui.js';
import { openEventDetail } from './events.js';
import { openTaskModal } from './tasks.js';
import { openDetailModal as openLedgerDetailModal } from './ledger.js';
import { openNoteViewModal } from './notes.js';
import { openDiaryViewModal } from './diary.js';

const CSS_ID    = 'pendo-search-redesign-styles';
const PAGE_SIZE = 20;

const TYPE_CONFIG = Object.freeze({
    event: { label: '日程', icon: '📅' },
    task: { label: '待办', icon: '✅' },
    ledger: { label: '记账', icon: '💰' },
    note: { label: '笔记', icon: '📝' },
    diary: { label: '日记', icon: '📔' },
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
    { label: '找逾期任务', query: '截止', type: 'task' },
    { label: '找餐饮支出', query: '餐饮', type: 'ledger' },
    { label: '找读书笔记', query: '阅读', type: 'note' },
    { label: '找最近日记', query: '今天', type: 'diary' },
];

let _container              = null;
let _query                  = '';
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
    if (item.type === 'event' && item.collection?.title) {
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
            item.location || item.collection?.location ? `📍 ${item.location || item.collection.location}` : '',
            item.category || item.collection?.category || '',
        ].filter(Boolean);
    }
    if (item.type === 'task') {
        return [
            item.status || '',
            item.priority != null ? `优先级 ${item.priority}` : '',
            item.plan_date ? `计划 ${formatZonedDateTime(item.plan_date, '')}` : '',
            item.deadline_at ? `截止 ${formatZonedDateTime(item.deadline_at, '')}` : '',
            item.category || '',
        ].filter(Boolean);
    }
    if (item.type === 'ledger') {
        const txType  = item.transaction_type || 'expense';
        const sign    = txType === 'income' ? '+' : txType === 'transfer' ? '↔ ' : '-';
        const amount  = item.amount != null ? `${sign}¥${item.amount.toFixed(2)}` : '';
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
            item.mood ? `心情 ${item.mood}` : '',
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

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('search-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}

        /* 搜索头部与查询输入 */
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
        .search-tab:focus-visible,
        .search-chip:focus-visible,
        .search-card:focus-visible {
            outline: 3px solid rgba(100,116,139,0.24);
            outline-offset: 2px;
        }

        /* 汇总、分组与结果卡片 */
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
            width: 100%; appearance: none; text-align: left; color: inherit; font: inherit;
            padding: 14px; border-radius: 20px; border: 1px solid rgba(203,213,225,0.82); background: rgba(255,255,255,0.96);
            cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
        }
        .search-card:hover { transform: translateY(-1px); border-color: rgba(100,116,139,0.28); box-shadow: 0 14px 28px rgba(15,23,42,0.06); }
        .search-type-event { --search-type-color: #B45309; --search-type-soft: rgba(245,158,11,0.14); }
        .search-type-task { --search-type-color: #047857; --search-type-soft: rgba(16,185,129,0.14); }
        .search-type-ledger { --search-type-color: #DC2626; --search-type-soft: rgba(239,68,68,0.14); }
        .search-type-note { --search-type-color: #2563EB; --search-type-soft: rgba(59,130,246,0.14); }
        .search-type-diary { --search-type-color: #DB2777; --search-type-soft: rgba(236,72,153,0.14); }
        .search-card-icon {
            width: 42px; height: 42px; border-radius: 14px; display: inline-flex; align-items: center; justify-content: center;
            background: var(--search-type-soft); color: var(--search-type-color); font-size: 18px;
        }
        .search-card-body { display: block; min-width: 0; }
        .search-card-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .search-card-title { display: block; margin: 0; font-size: 15px; font-weight: 780; color: var(--color-text); }
        .search-card-badge { padding: 4px 8px; border-radius: 999px; background: var(--search-type-color); font-size: 11px; font-weight: 700; color: #fff; }
        .search-card-preview { display: block; margin-top: 6px; font-size: 13px; line-height: 1.65; color: var(--color-text-secondary); }
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
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .search-hero, .search-query-bar, .search-summary { grid-template-columns: 1fr; }
            .search-query-meta { justify-content: flex-start; }
            .search-card { grid-template-columns: auto minmax(0, 1fr); }
            .search-card-arrow { display: none; }
        `,
        )}
    `,
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
                data-suggest-query="${escapeHtml(item.query)}" data-suggest-type="${escapeHtml(item.type)}">
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
    if (!_query.trim() || !_hasSearched) return '';
    const counts       = resultCounts();
    const activeTypes  = TYPE_ORDER.filter((type) => counts[type]);
    const visibleCount = _results.length;
    const total        = nonNegativeInteger(_total);
    const summary      =         total > visibleCount ? `当前共命中 ${total} 条，本页展示 ${visibleCount} 条` : `当前共命中 ${total} 条`;
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
    const cfg      = TYPE_CONFIG[item.type];
    const title    = itemTitle(item);
    const preview  = itemPreview(item);
    const metadata = itemMeta(item);
    return `
        <button class="search-card search-type-${item.type}" type="button"
                data-open-result="${escapeHtml(item.id)}" aria-label="${escapeHtml(`打开${cfg.label}：${title}`)}">
            <span class="search-card-icon" aria-hidden="true">${cfg.icon}</span>
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
        return '<div class="search-empty" role="status"><div class="search-empty-icon">⏳</div><div>正在搜索...</div></div>';
    if (!_query.trim()) {
        return `<div class="search-empty"><div class="search-empty-icon">🔎</div><div>输入关键词开始检索，下面的建议可以直接点。</div></div>`;
    }
    if (_hasSearched && !_results.length) {
        return `<div class="search-empty"><div class="search-empty-icon">🧭</div><div>没有找到结果。试试换关键词、切类型，或先清空主题切片。</div></div>`;
    }
    return `<div class="search-results-stack">${groupedResults()
        .map(
            (group) => `
        <section class="search-group">
            <div class="search-group-head">
                <div class="search-group-title">${TYPE_CONFIG[group.type].icon} ${TYPE_CONFIG[group.type].label}</div>
                <span class="search-group-count">${group.items.length}</span>
            </div>
            <div class="search-card-list">${group.items.map(renderCard).join('')}</div>
        </section>`,
        )
        .join('')}</div>`;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    const typeCounts  = resultCounts();
    const activeTypes = TYPE_ORDER.filter((type) => typeCounts[type]);
    _container.innerHTML = `
        <div class="search-shell">
            <section class="search-hero">
                <div>
                    <h2><span class="search-hero-icon" aria-hidden="true">🔍</span><span>搜索</span></h2>
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
    return JSON.stringify([query, page, _activeType, _activeCategory, _activeCategoryField, _activeCategoryTypeHint]);
}

async function doSearch({ resetPage = false, force = false } = {}) {
    if (resetPage) _page = 1;
    const q = _query.trim();
    if (!q) {
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
            _activeCategory = '';
            _activeCategoryField = 'category';
            _activeCategoryTypeHint = '';
            if (_query.trim()) await doSearch({ resetPage: true });
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
            if (_query.trim()) await doSearch({ resetPage: true });
            else renderPage();
        };
    });

    root.querySelectorAll('.search-suggestion[data-suggest-query]').forEach((button) => {
        button.onclick = async () => {
            _query = textValue(button.dataset.suggestQuery).trim();
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
        if (!_query.trim() || !_hasSearched) return;
        await doSearch({ force: true });
    });
    if (_query.trim()) await doSearch({ resetPage: true, force: true });
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
}
