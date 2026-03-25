import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { navigate } from '../router.js';

// ── constants ─────────────────────────────────────────────────────────────────

const CSS_ID = 'pendo-search-styles';

const TYPE_CONFIG = {
    event:  { label: '日程', icon: '📅', color: '#F59E0B', route: 'events' },
    task:   { label: '待办', icon: '✅', color: '#10B981', route: 'tasks' },
    ledger: { label: '记账', icon: '💰', color: '#EF4444', route: 'ledger' },
    note:   { label: '笔记', icon: '📝', color: '#3B82F6', route: 'notes' },
    diary:  { label: '日记', icon: '📔', color: '#EC4899', route: 'diary' },
};

const FILTER_TABS = [
    { value: '',       label: '全部' },
    { value: 'event',  label: '日程' },
    { value: 'task',   label: '待办' },
    { value: 'ledger', label: '记账' },
    { value: 'note',   label: '笔记' },
    { value: 'diary',  label: '日记' },
];

// ── module state ──────────────────────────────────────────────────────────────

let _container     = null;
let _query         = '';
let _activeType    = '';
let _results       = [];
let _loading       = false;
let _debounceTimer = null;
let _hasSearched   = false;

// ── helpers ───────────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${padZ(d.getMonth() + 1)}-${padZ(d.getDate())} ${padZ(d.getHours())}:${padZ(d.getMinutes())}`;
}

function contentPreview(content) {
    if (!content) return '';
    const text = content.trim();
    return text.length <= 80 ? text : text.slice(0, 80) + '...';
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .search-page {
            padding: 24px;
            max-width: 860px;
            margin: 0 auto;
        }

        .search-page-header {
            margin-bottom: 20px;
        }

        .search-page-title {
            font-size: 20px;
            font-weight: 700;
            color: var(--color-search, #6B7280);
            margin-bottom: 2px;
        }

        .search-page-subtitle {
            font-size: 13px;
            color: var(--color-text-secondary);
        }

        .search-input-wrap {
            position: relative;
            margin-bottom: 16px;
        }

        .search-input-icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 16px;
            color: var(--color-text-tertiary);
            pointer-events: none;
        }

        .search-input {
            width: 100%;
            box-sizing: border-box;
            padding: 11px 14px 11px 42px;
            font-size: 15px;
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            background: var(--color-surface);
            color: var(--color-text);
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
        }

        .search-input:focus {
            border-color: var(--color-search, #6B7280);
            box-shadow: 0 0 0 3px rgba(107,114,128,0.12);
        }

        .search-filter-tabs {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin-bottom: 20px;
        }

        .search-tab {
            padding: 5px 14px;
            border-radius: 20px;
            border: 1px solid var(--color-border);
            background: var(--color-surface);
            color: var(--color-text-secondary);
            font-size: 13px;
            cursor: pointer;
            transition: background 0.12s, color 0.12s, border-color 0.12s;
            white-space: nowrap;
        }

        .search-tab:hover {
            border-color: var(--color-search, #6B7280);
            color: var(--color-text);
        }

        .search-tab.active {
            background: var(--color-search, #6B7280);
            border-color: var(--color-search, #6B7280);
            color: #fff;
            font-weight: 600;
        }

        .search-meta {
            font-size: 13px;
            color: var(--color-text-secondary);
            margin-bottom: 14px;
        }

        .search-results {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .search-result-card {
            display: flex;
            align-items: flex-start;
            gap: 14px;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 14px 16px;
            cursor: pointer;
            transition: box-shadow 0.15s, border-color 0.15s, transform 0.1s;
        }

        .search-result-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.10);
            transform: translateY(-1px);
        }

        .search-result-icon {
            font-size: 22px;
            flex-shrink: 0;
            line-height: 1;
            margin-top: 1px;
        }

        .search-result-body {
            flex: 1;
            min-width: 0;
        }

        .search-result-top {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 4px;
            flex-wrap: wrap;
        }

        .search-result-title {
            font-size: 15px;
            font-weight: 600;
            color: var(--color-text);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .search-result-type-badge {
            font-size: 11px;
            font-weight: 500;
            border-radius: 4px;
            padding: 2px 7px;
            color: #fff;
            white-space: nowrap;
            flex-shrink: 0;
        }

        .search-result-preview {
            font-size: 13px;
            color: var(--color-text-secondary);
            line-height: 1.5;
            margin-bottom: 6px;
            word-break: break-word;
        }

        .search-result-footer {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .search-result-date {
            font-size: 11px;
            color: var(--color-text-tertiary);
        }

        .search-result-category {
            font-size: 11px;
            background: var(--color-border);
            color: var(--color-text-secondary);
            border-radius: 4px;
            padding: 2px 6px;
        }

        .search-result-arrow {
            margin-left: auto;
            font-size: 14px;
            color: var(--color-text-tertiary);
            flex-shrink: 0;
        }

        .search-empty {
            text-align: center;
            padding: 60px 0;
            color: var(--color-text-tertiary);
        }

        .search-empty-icon {
            font-size: 40px;
            margin-bottom: 12px;
        }

        .search-empty-text {
            font-size: 14px;
        }

        .search-loading {
            text-align: center;
            padding: 40px 0;
            color: var(--color-text-secondary);
            font-size: 14px;
        }
    `;
    document.head.appendChild(style);
}

// ── render ────────────────────────────────────────────────────────────────────

function renderFilterTabs() {
    return `
        <div class="search-filter-tabs" id="search-filter-tabs">
            ${FILTER_TABS.map(tab => {
                const cfg = tab.value ? TYPE_CONFIG[tab.value] : null;
                const isActive = _activeType === tab.value;
                let activeStyle = '';
                if (isActive && cfg) {
                    activeStyle = `style="background:${cfg.color};border-color:${cfg.color};"`;
                }
                return `
                    <button
                        class="search-tab${isActive ? ' active' : ''}"
                        data-type="${tab.value}"
                        ${activeStyle}
                    >${tab.label}</button>
                `;
            }).join('')}
        </div>
    `;
}

function renderResultCard(item) {
    const cfg     = TYPE_CONFIG[item.type] || { label: item.type, icon: '❓', color: '#9CA3AF', route: '' };
    const preview = contentPreview(item.content);
    const date    = formatDate(item.created_at);

    return `
        <div class="search-result-card" data-id="${escapeHtml(String(item.id))}" data-type="${escapeHtml(item.type)}" data-route="${escapeHtml(cfg.route)}">
            <div class="search-result-icon">${cfg.icon}</div>
            <div class="search-result-body">
                <div class="search-result-top">
                    <span class="search-result-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title) || '(无标题)'}</span>
                    <span class="search-result-type-badge" style="background:${cfg.color};">${cfg.label}</span>
                </div>
                ${preview ? `<div class="search-result-preview">${escapeHtml(preview)}</div>` : ''}
                <div class="search-result-footer">
                    ${item.category ? `<span class="search-result-category">${escapeHtml(item.category)}</span>` : ''}
                    ${date ? `<span class="search-result-date">${date}</span>` : ''}
                </div>
            </div>
            <span class="search-result-arrow">→</span>
        </div>
    `;
}

function renderResults() {
    if (_loading) {
        return `<div class="search-loading">搜索中...</div>`;
    }

    if (!_query.trim()) {
        return `
            <div class="search-empty">
                <div class="search-empty-icon">🔍</div>
                <div class="search-empty-text">输入关键词开始搜索</div>
            </div>
        `;
    }

    if (_hasSearched && _results.length === 0) {
        return `
            <div class="search-empty">
                <div class="search-empty-icon">😶</div>
                <div class="search-empty-text">未找到相关结果</div>
            </div>
        `;
    }

    return `<div class="search-results">${_results.map(renderResultCard).join('')}</div>`;
}

function renderPage() {
    if (!_container) return;

    ensureStyles();

    const countText = (_hasSearched && _query.trim())
        ? `找到 <strong>${_results.length}</strong> 条结果`
        : '';

    _container.innerHTML = `
        <div class="search-page">
            <div class="search-page-header">
                <div class="search-page-title">🔍 搜索</div>
                <div class="search-page-subtitle">跨类型全局搜索</div>
            </div>

            <div class="search-input-wrap">
                <span class="search-input-icon">🔍</span>
                <input
                    type="text"
                    id="search-input"
                    class="search-input"
                    placeholder="搜索日程、待办、记账、笔记、日记..."
                    value="${escapeHtml(_query)}"
                    autocomplete="off"
                />
            </div>

            ${renderFilterTabs()}

            ${countText ? `<div class="search-meta">${countText}</div>` : ''}

            <div id="search-results-area">
                ${renderResults()}
            </div>
        </div>
    `;

    attachListeners();

    // Auto-focus input
    const input = _container.querySelector('#search-input');
    if (input) {
        input.focus();
        // Place cursor at end
        const len = input.value.length;
        input.setSelectionRange(len, len);
    }
}

function updateResultsArea() {
    if (!_container) return;
    const area = _container.querySelector('#search-results-area');
    if (area) area.innerHTML = renderResults();

    const metaEl = _container.querySelector('.search-meta');
    const countText = (_hasSearched && _query.trim())
        ? `找到 <strong>${_results.length}</strong> 条结果`
        : '';
    if (metaEl) {
        metaEl.innerHTML = countText;
    } else if (countText) {
        // Insert meta before results area if it didn't exist
        const resultsArea = _container.querySelector('#search-results-area');
        if (resultsArea) {
            const div = document.createElement('div');
            div.className = 'search-meta';
            div.innerHTML = countText;
            resultsArea.before(div);
        }
    }
}

// ── listeners ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;

    const input = _container.querySelector('#search-input');
    if (input) {
        input.addEventListener('input', () => {
            _query = input.value;
            clearTimeout(_debounceTimer);
            if (!_query.trim()) {
                _results    = [];
                _hasSearched = false;
                _loading    = false;
                updateResultsArea();
                return;
            }
            _loading = true;
            updateResultsArea();
            _debounceTimer = setTimeout(() => doSearch(), 300);
        });
    }

    const tabsEl = _container.querySelector('#search-filter-tabs');
    if (tabsEl) {
        tabsEl.addEventListener('click', (e) => {
            const btn = e.target.closest('.search-tab');
            if (!btn) return;
            _activeType = btn.dataset.type;
            // Update active state visually without full re-render
            tabsEl.querySelectorAll('.search-tab').forEach(t => {
                const type = t.dataset.type;
                const cfg  = type ? TYPE_CONFIG[type] : null;
                if (type === _activeType) {
                    t.classList.add('active');
                    if (cfg) t.style.cssText = `background:${cfg.color};border-color:${cfg.color};`;
                } else {
                    t.classList.remove('active');
                    t.style.cssText = '';
                }
            });
            if (_query.trim()) doSearch();
        });
    }

    const resultsArea = _container.querySelector('#search-results-area');
    if (resultsArea) {
        resultsArea.addEventListener('click', (e) => {
            const card = e.target.closest('.search-result-card');
            if (!card) return;
            const route = card.dataset.route;
            if (route) navigate(route);
        });
    }
}

// ── API ───────────────────────────────────────────────────────────────────────

async function doSearch() {
    const q = _query.trim();
    if (!q) {
        _results    = [];
        _hasSearched = false;
        _loading    = false;
        updateResultsArea();
        return;
    }

    _loading = true;
    updateResultsArea();

    try {
        const params = { q, limit: 50 };
        if (_activeType) params.type = _activeType;
        const res = await api.get('/search', params);
        _results    = (res.data && res.data.items) ? res.data.items : [];
        _hasSearched = true;
    } catch (err) {
        _results    = [];
        _hasSearched = true;
        showToast('搜索失败：' + err.message, 'error');
    }

    _loading = false;
    updateResultsArea();
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container   = container;
    _results     = [];
    _hasSearched = false;
    _loading     = false;

    renderPage();
}

export function destroy() {
    clearTimeout(_debounceTimer);
    _debounceTimer = null;
    _container     = null;
    _results       = [];
    _hasSearched   = false;
    _loading       = false;
}

export function onRouteEnter(params) {
    const q = params ? params.get('q') : '';
    if (q) {
        _query = q;
        // Update input value if DOM is ready
        if (_container) {
            const input = _container.querySelector('#search-input');
            if (input) input.value = _query;
        }
        doSearch();
    }
}
