import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { formatDateTime, formatMonthDay, previewText } from '../utils/format.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const PAGE_SIZE = 18;
const CSS_ID = 'pendo-notes-styles';

const NOTE_FIELDS = [
    { name: 'title', label: '标题', type: 'text', required: true },
    { name: 'category', label: '分类', type: 'text', placeholder: '未分类' },
    { name: 'tags', label: '标签', type: 'text', placeholder: '逗号分隔，如：工作,阅读' },
    { name: 'content', label: '内容', type: 'textarea', rows: 10 },
];

let _container = null;
let _items = [];
let _overview = null;
let _total = 0;
let _page = 1;
let _loading = false;
let _dataChangedHandler = null;
let _filters = {
    category: '',
    tag: '',
};

function todayKey() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

function formatDate(value) {
    return formatMonthDay(value);
}

function tagList(value) {
    const list = Array.isArray(value) ? value : [];
    const seen = new Set();
    return list
        .map((tag) => String(tag || '').trim())
        .filter(Boolean)
        .filter((tag) => {
            const key = tag.toLowerCase();
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
}

function tagsToString(tags) {
    return tagList(tags).join(', ');
}

function tagsFromInput(value) {
    return tagList(String(value || '').split(','));
}

function noteWordCount(note) {
    return String(note?.content || '').trim().length;
}

function categoryOptions() {
    const categories = _overview?.all_categories || [];
    return [{ value: '', label: '全部分类' }, ...categories.map((category) => ({ value: category, label: category }))];
}

function ensureStyles() {
    injectStyles(CSS_ID, `
        ${pageShellCss('notes-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.NARROW })}
        .notes-hero {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 18px;
            align-items: center;
            padding: 24px 26px;
            border-radius: 28px;
            margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(59,130,246,0.18), transparent 32%),
                radial-gradient(circle at bottom left, rgba(14,165,233,0.12), transparent 24%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(239,246,255,0.95));
            border: 1px solid rgba(59,130,246,0.14);
            box-shadow: 0 18px 40px rgba(37,99,235,0.06);
        }
        .notes-hero h2 { margin: 0; font-size: 30px; font-weight: 820; letter-spacing: -0.03em; color: #1d4ed8; }
        .notes-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .notes-hero-tags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
        .notes-hero-tag {
            display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 999px;
            background: rgba(59,130,246,0.08); color: #1d4ed8; font-size: 12px; font-weight: 700;
        }
        .notes-hero-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
        .notes-stack { display: flex; flex-direction: column; gap: 18px; }
        .notes-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
        .notes-summary-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.94));
            border: 1px solid rgba(59,130,246,0.12); border-radius: 22px; padding: 18px;
            box-shadow: 0 14px 30px rgba(37,99,235,0.05);
        }
        .notes-summary-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .notes-summary-value { margin-top: 10px; font-size: 30px; font-weight: 820; line-height: 1.04; letter-spacing: -0.03em; color: #0f172a; }
        .notes-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); }
        .notes-layout { display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(300px, 0.92fr); gap: 16px; align-items: start; }
        .notes-layout-main,
        .notes-layout-side { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
        .notes-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.95));
            border: 1px solid rgba(59,130,246,0.12); border-radius: 24px; box-shadow: 0 16px 34px rgba(37,99,235,0.05);
            overflow: hidden;
        }
        .notes-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 18px 20px 0; }
        .notes-panel-head h3 { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); letter-spacing: -0.02em; }
        .notes-panel-head p { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .notes-panel-body { padding: 16px 20px 20px; }
        .notes-meter {
            display: grid; grid-template-columns: repeat(14, minmax(0, 1fr)); gap: 8px; align-items: end;
        }
        .notes-meter-col { display: flex; flex-direction: column; align-items: center; gap: 4px; }
        .notes-meter-value { font-size: 10px; font-weight: 700; color: var(--color-text); }
        .notes-meter-stick {
            width: 100%; min-height: 8px; border-radius: 999px 999px 10px 10px;
            background: linear-gradient(180deg, rgba(59,130,246,0.9), rgba(14,165,233,0.46));
        }
        .notes-meter-label { font-size: 10px; color: var(--color-text-secondary); }
        .notes-cadence-panel .notes-panel-body { padding-top: 10px; padding-bottom: 16px; }
        .notes-category-list { display: flex; flex-direction: column; gap: 12px; }
        .notes-category-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }
        .notes-category-top { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; margin-bottom: 6px; }
        .notes-category-name { font-weight: 700; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .notes-category-count { color: var(--color-text-secondary); font-weight: 700; }
        .notes-category-track { height: 10px; border-radius: 999px; background: rgba(191,219,254,0.42); overflow: hidden; }
        .notes-category-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, rgba(59,130,246,0.88), rgba(14,165,233,0.55)); }
        .notes-tag-list { display: flex; flex-wrap: wrap; gap: 8px; }
        .notes-tag-chip {
            display: inline-flex; align-items: center; gap: 6px; padding: 8px 12px; border-radius: 999px;
            background: rgba(255,255,255,0.84); border: 1px solid rgba(59,130,246,0.12); color: #1d4ed8;
            font-size: 12px; font-weight: 700; cursor: pointer;
        }
        .notes-tag-chip:hover { background: rgba(219,234,254,0.8); }
        .notes-filter-bar {
            display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 0.46fr) auto; gap: 12px;
            padding: 16px 18px; border-radius: 22px; background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.94));
            border: 1px solid rgba(59,130,246,0.12); box-shadow: 0 14px 30px rgba(37,99,235,0.04);
        }
        .notes-filter-field { display: flex; flex-direction: column; gap: 6px; }
        .notes-filter-field label { font-size: 11px; font-weight: 800; color: var(--color-text-secondary); letter-spacing: 0.05em; text-transform: uppercase; }
        .notes-filter-field input {
            width: 100%; height: 40px; border-radius: 14px; border: 1px solid rgba(59,130,246,0.14); background: rgba(255,255,255,0.92);
            padding: 0 14px; font-size: 13px; color: var(--color-text);
        }
        .notes-filter-field input:focus {
            outline: none; border-color: var(--color-notes, #3B82F6); box-shadow: 0 0 0 3px rgba(59,130,246,0.12);
        }
        .notes-filter-bar .pselect-trigger {
            height: 40px;
            padding: 0 14px;
            border-radius: 14px;
            background: rgba(255,255,255,0.92);
        }
        .notes-filter-bar .pselect:hover .pselect-trigger,
        .notes-filter-bar .pselect.pselect-open .pselect-trigger {
            background: rgba(255,255,255,0.97);
        }
        .notes-filter-actions { display: flex; align-items: flex-end; justify-content: flex-end; gap: 10px; }
        .notes-workspace {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.95));
            border: 1px solid rgba(59,130,246,0.12); border-radius: 26px; box-shadow: 0 18px 34px rgba(37,99,235,0.05);
            overflow: hidden;
        }
        .notes-workspace-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid rgba(191,219,254,0.42); }
        .notes-workspace-title { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); }
        .notes-workspace-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .notes-workspace-body { padding: 18px 20px 22px; }
        .notes-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
        .note-card {
            display: flex; flex-direction: column; min-height: 196px; border-radius: 18px; overflow: hidden; cursor: pointer;
            border: 1px solid rgba(59,130,246,0.12); background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(239,246,255,0.9));
            box-shadow: 0 12px 26px rgba(37,99,235,0.04); transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
        }
        .note-card:hover { transform: translateY(-2px); border-color: rgba(59,130,246,0.24); box-shadow: 0 18px 32px rgba(37,99,235,0.08); }
        .note-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 14px 14px 8px; }
        .note-card-title { margin: 0; font-size: 16px; font-weight: 760; color: var(--color-text); line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .note-card-category {
            display: inline-flex; align-items: center; height: 26px; padding: 0 10px; border-radius: 999px; background: rgba(59,130,246,0.10);
            color: #1d4ed8; font-size: 11px; font-weight: 700; flex-shrink: 0;
        }
        .note-card-body { padding: 0 14px; flex: 1; }
        .note-card-preview { font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); min-height: 52px; word-break: break-word; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .note-card-footer { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 8px; }
        .note-card-tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; }
        .note-tag {
            display: inline-flex; align-items: center; height: 26px; padding: 0 10px; border-radius: 999px;
            background: rgba(219,234,254,0.8); color: #1e40af; font-size: 11px; font-weight: 700;
        }
        .note-card-meta { display: flex; justify-content: space-between; gap: 8px; align-items: center; font-size: 11px; color: var(--color-text-secondary); }
        .notes-empty {
            padding: 46px 18px; border-radius: 20px; text-align: center; background: rgba(248,250,252,0.82);
            border: 1px dashed rgba(148,163,184,0.26); color: var(--color-text-secondary);
        }
        .notes-pagination { margin-top: 18px; }
        .note-view-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-bottom: 14px; border-bottom: 1px solid rgba(226,232,240,0.8); margin-bottom: 14px; }
        .note-view-content { white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.8; color: var(--color-text); max-height: 60vh; overflow-y: auto; }
        .note-view-secondary { font-size: 12px; color: var(--color-text-secondary); margin-left: auto; }
        ${mediaMax(BREAKPOINTS.WIDE, `
            .notes-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .notes-layout { grid-template-columns: 1fr; }
            .notes-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        `)}
        ${mediaMax(BREAKPOINTS.NARROW, `
            .notes-hero { grid-template-columns: 1fr; padding: 22px 20px; }
            .notes-hero-actions { align-items: flex-start; }
            .notes-summary-grid { grid-template-columns: 1fr; }
            .notes-filter-bar { grid-template-columns: 1fr; }
            .notes-filter-actions { justify-content: flex-start; }
            .notes-grid { grid-template-columns: 1fr; }
            .notes-meter { gap: 6px; }
            .notes-cadence-panel .notes-panel-body { padding-bottom: 14px; }
        `)}
    `);
}

async function fetchOverview() {
    const params = { today: todayKey() };
    if (_filters.category) params.category = _filters.category;
    if (_filters.tag) params.tags = _filters.tag;
    const res = await api.get('/stats/notes/overview', params);
    return res.data || null;
}

async function fetchNotes(page = 1) {
    const params = {
        type: 'note',
        page,
        page_size: PAGE_SIZE,
        sort: 'updated_at',
        order: 'desc',
    };
    if (_filters.category) params.category = _filters.category;
    if (_filters.tag) params.tags = _filters.tag;
    const res = await api.get('/items', params);
    return {
        items: res?.data?.items || [],
        total: res?.data?.total || 0,
    };
}

function renderHero() {
    const summary = _overview?.summary || {};
    return `
        <section class="notes-hero">
            <div>
                <h2>📝 笔记</h2>
                <p>集中整理灵感、摘录和工作草稿。</p>
                <div class="notes-hero-tags">
                    <span class="notes-hero-tag">${summary.total_count || 0} 条笔记</span>
                    <span class="notes-hero-tag">近 7 天 ${summary.week_new_count || 0} 条新增</span>
                    <span class="notes-hero-tag">${_filters.category || '全部分类'}</span>
                </div>
            </div>
            <div class="notes-hero-actions">
                <button class="btn btn-primary" id="notes-add-top">＋ 新建笔记</button>
            </div>
        </section>
    `;
}

function renderSummary() {
    const summary = _overview?.summary || {};
    const taggedRate = Math.round((summary.tagged_rate || 0) * 100);
    return `
        <section class="notes-summary-grid">
            <div class="notes-summary-card">
                <div class="notes-summary-label">总笔记数</div>
                <div class="notes-summary-value">${summary.total_count || 0}</div>
                <div class="notes-summary-meta">当前知识库累计条目</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">本周新增</div>
                <div class="notes-summary-value">${summary.week_new_count || 0}</div>
                <div class="notes-summary-meta">最近 7 天的新增速度</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">平均字数</div>
                <div class="notes-summary-value">${Math.round(summary.average_length || 0)}</div>
                <div class="notes-summary-meta">当前笔记概况</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">已打标签</div>
                <div class="notes-summary-value">${taggedRate}%</div>
                <div class="notes-summary-meta">便于后续回看与筛选</div>
            </div>
        </section>
    `;
}

function renderCadencePanel() {
    const cadence = _overview?.cadence || [];
    const maxCount = Math.max(1, ...cadence.map((item) => item.count || 0));
    return `
        <section class="notes-panel notes-cadence-panel">
            <div class="notes-panel-head">
                <div>
                    <h3>书写节奏</h3>
                    <p>查看最近两周的记录频率。</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-meter">
                    ${cadence.map((item) => `
                        <div class="notes-meter-col">
                            <div class="notes-meter-value">${item.count}</div>
                            <div class="notes-meter-stick" style="height:${8 + (item.count / maxCount) * 42}px;"></div>
                            <div class="notes-meter-label">${item.label}</div>
                        </div>`).join('')}
                </div>
            </div>
        </section>
    `;
}

function renderCategoryPanel() {
    const categories = _overview?.categories || [];
    if (!categories.length) {
        return `
            <section class="notes-panel">
                <div class="notes-panel-head">
                    <div>
                        <h3>分类分布</h3>
                        <p>写下笔记后，这里会显示分类分布。</p>
                    </div>
                </div>
                <div class="notes-panel-body"><div class="notes-empty">当前没有笔记分类数据。</div></div>
            </section>
        `;
    }
    return `
        <section class="notes-panel">
            <div class="notes-panel-head">
                <div>
                    <h3>分类分布</h3>
                    <p>查看当前笔记主要集中在哪些主题。</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-category-list">
                    ${categories.map((item) => `
                        <div class="notes-category-row">
                            <div>
                                <div class="notes-category-top">
                                    <span class="notes-category-name">${escapeHtml(item.category)}</span>
                                    <span class="notes-category-count">${item.count} 条</span>
                                </div>
                                <div class="notes-category-track">
                                    <div class="notes-category-fill" style="width:${Math.max(10, Math.round(item.share * 100))}%;"></div>
                                </div>
                            </div>
                            <div class="note-tag">${Math.round(item.share * 100)}%</div>
                        </div>`).join('')}
                </div>
            </div>
        </section>
    `;
}

function renderTagPanel() {
    const tags = _overview?.hot_tags || [];
    if (!tags.length) {
        return `
            <section class="notes-panel">
                <div class="notes-panel-head">
                    <div>
                        <h3>热门标签</h3>
                        <p>添加标签后，这里会显示常用主题。</p>
                    </div>
                </div>
                <div class="notes-panel-body"><div class="notes-empty">当前还没有标签数据。</div></div>
            </section>
        `;
    }
    return `
        <section class="notes-panel">
            <div class="notes-panel-head">
                <div>
                    <h3>热门标签</h3>
                    <p>点击标签可直接筛选当前笔记。</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-tag-list">
                    ${tags.map((item) => `
                        <button class="notes-tag-chip" data-tag="${escapeHtml(item.tag)}" type="button">
                            <span>#${escapeHtml(item.tag)}</span>
                            <span>${item.count}</span>
                        </button>`).join('')}
                </div>
            </div>
        </section>
    `;
}

function renderFilters() {
    return `
        <section class="notes-filter-bar">
            <div class="notes-filter-field">
                <label>标签筛选</label>
                <input id="notes-filter-tag" type="text" placeholder="输入标签，例如：阅读" value="${escapeHtml(_filters.tag)}">
            </div>
            <div class="notes-filter-field">
                <label>分类</label>
                ${renderCustomSelect({
                    id: 'notes-filter-category',
                    options: categoryOptions(),
                    selected: _filters.category,
                    className: 'pselect-block pselect-theme-notes',
                    placeholder: '全部分类',
                })}
            </div>
            <div class="notes-filter-actions">
                <button class="btn btn-secondary" id="notes-filter-reset" type="button">重置筛选</button>
            </div>
        </section>
    `;
}

function renderNoteCard(note) {
    const tags = tagList(note.tags);
    const preview = previewText(note.content, 118);
    return `
        <article class="note-card" data-id="${note.id}">
            <div class="note-card-head">
                <h4 class="note-card-title">${escapeHtml(note.title || '(无标题)')}</h4>
                <span class="note-card-category">${escapeHtml(note.category || '未分类')}</span>
            </div>
            <div class="note-card-body">
                <div class="note-card-preview">${preview ? escapeHtml(preview) : '<span style="opacity:0.45;">（无内容）</span>'}</div>
            </div>
            <div class="note-card-footer">
                <div class="note-card-tags">${tags.length ? tags.slice(0, 4).map((tag) => `<span class="note-tag">#${escapeHtml(tag)}</span>`).join('') : '<span class="note-tag" style="opacity:0.72;">未打标签</span>'}</div>
                <div class="note-card-meta">
                    <span>${formatDate(note.updated_at || note.created_at)}</span>
                    <span>${noteWordCount(note)} 字</span>
                </div>
            </div>
        </article>
    `;
}

function renderGrid() {
    if (!_items.length) {
        return `<div class="notes-empty">当前筛选下没有笔记。试试清空标签或切换分类。</div>`;
    }
    return `<div class="notes-grid">${_items.map(renderNoteCard).join('')}</div>`;
}

function renderWorkspace() {
    return `
        <section class="notes-workspace">
            <div class="notes-workspace-head">
                <div>
                    <h3 class="notes-workspace-title">最近笔记</h3>
                    <p class="notes-workspace-subtitle">按最近更新浏览当前笔记。</p>
                </div>
                <div class="note-tag">${_total} 条匹配</div>
            </div>
            <div class="notes-workspace-body">
                ${renderGrid()}
                <div id="notes-pagination" class="notes-pagination"></div>
            </div>
        </section>
    `;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();

    if (_loading && !_overview) {
        _container.innerHTML = `<div class="notes-shell"><div class="notes-empty">正在加载笔记空间...</div></div>`;
        return;
    }

    _container.innerHTML = `
        <div class="notes-shell">
            ${renderHero()}
            <div class="notes-stack">
                ${renderSummary()}
                <section class="notes-layout">
                    <div class="notes-layout-main">
                        ${renderCadencePanel()}
                        ${renderTagPanel()}
                    </div>
                    <div class="notes-layout-side">
                        ${renderCategoryPanel()}
                    </div>
                </section>
                ${renderFilters()}
                ${renderWorkspace()}
            </div>
        </div>
    `;

    const paginationEl = _container.querySelector('#notes-pagination');
    if (paginationEl) {
        renderPagination(paginationEl, {
            page: _page,
            pageSize: PAGE_SIZE,
            total: _total,
            onChange: async (page) => {
                _page = page;
                await loadAndRender();
            },
        });
    }

    attachListeners();
}

async function loadAndRender() {
    _loading = true;
    renderPage();
    try {
        const [overview, list] = await Promise.all([
            fetchOverview(),
            fetchNotes(_page),
        ]);
        _overview = overview;
        _items = list.items;
        _total = list.total;
    } catch (err) {
        _overview = null;
        _items = [];
        _total = 0;
        showToast(`加载笔记失败：${err.message}`, 'error');
    } finally {
        _loading = false;
    }
    renderPage();
}

function openNoteViewModal(note) {
    const tags = tagList(note.tags);
    const bodyHTML = `
        <div class="note-view-meta">
            <span class="note-card-category">${escapeHtml(note.category || '未分类')}</span>
            ${tags.map((tag) => `<span class="note-tag">#${escapeHtml(tag)}</span>`).join('')}
            <span class="note-view-secondary">更新于 ${formatDateTime(note.updated_at || note.created_at)}</span>
        </div>
        <div class="note-view-content">${escapeHtml(note.content || '')}</div>
    `;
    const footer = `
        <button class="btn btn-danger btn-sm" id="note-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary" id="note-close">关闭</button>
        <button class="btn btn-primary" id="note-edit">编辑</button>
    `;
    const content = showModal(note.title || '(无标题)', bodyHTML, { footer });
    content.querySelector('#note-close').onclick = closeModal;
    content.querySelector('#note-edit').onclick = () => {
        closeModal();
        openNoteFormModal(note);
    };
    content.querySelector('#note-delete').onclick = async () => {
        closeModal();
        const confirmed = await showConfirmModal({
            title: '删除笔记',
            message: `确定要删除“${note.title || '这条笔记'}”吗？删除后内容将无法恢复。`,
            confirmText: '删除',
            cancelText: '返回详情',
            tone: 'danger',
        });
        if (!confirmed) {
            openNoteViewModal(note);
            return;
        }
        try {
            await api.delete(`/items/${note.id}`);
            showToast('笔记已删除', 'success');
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'note' } }));
            await loadAndRender();
        } catch (err) {
            showToast(`删除失败：${err.message}`, 'error');
        }
    };
}

function openNoteFormModal(existing = null) {
    const isEdit = Boolean(existing);
    const fields = NOTE_FIELDS.map((field) => {
        let value = '';
        if (existing) {
            if (field.name === 'tags') value = tagsToString(existing.tags);
            else value = existing[field.name] ?? '';
        }
        return { ...field, value };
    });

    const content = showModal(
        isEdit ? '编辑笔记' : '新建笔记',
        `<form id="note-form">${buildFormHTML(fields)}</form>`,
        {
            footer: `
                ${isEdit ? '<button class="btn btn-danger btn-sm" id="note-modal-delete" style="margin-right:auto;">删除</button>' : ''}
                <button class="btn btn-secondary" id="note-modal-cancel">取消</button>
                <button class="btn btn-primary" id="note-modal-save">保存</button>
            `,
        },
    );

    initFormInteractions(content);
    content.querySelector('#note-modal-cancel').onclick = closeModal;

    if (isEdit) {
        content.querySelector('#note-modal-delete').onclick = async () => {
            closeModal();
            const confirmed = await showConfirmModal({
                title: '删除笔记',
                message: `确定要删除“${existing.title || '这条笔记'}”吗？删除后内容将无法恢复。`,
                confirmText: '删除',
                cancelText: '返回编辑',
                tone: 'danger',
            });
            if (!confirmed) {
                openNoteFormModal(existing);
                return;
            }
            try {
                await api.delete(`/items/${existing.id}`);
                showToast('笔记已删除', 'success');
                window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'note' } }));
                await loadAndRender();
            } catch (err) {
                showToast(`删除失败：${err.message}`, 'error');
            }
        };
    }

    content.querySelector('#note-modal-save').onclick = async () => {
        const form = content.querySelector('#note-form');
        const data = getFormData(form);
        if (!data.title) {
            showToast('请填写标题', 'warning');
            return;
        }
        data.tags = tagsFromInput(data.tags || '');
        try {
            if (isEdit) {
                await api.put(`/items/${existing.id}`, data);
                showToast('笔记已更新', 'success');
            } else {
                await api.post('/items', { type: 'note', ...data });
                showToast('笔记已创建', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'note' } }));
            await loadAndRender();
        } catch (err) {
            showToast(`保存失败：${err.message}`, 'error');
        }
    };
}

function attachListeners() {
    if (!_container) return;

    initCustomSelects(_container, {
        'notes-filter-category': async (value) => {
            _filters.category = value;
            _page = 1;
            await loadAndRender();
        },
    });

    const addTop = _container.querySelector('#notes-add-top');
    if (addTop) addTop.onclick = () => openNoteFormModal(null);

    const reset = _container.querySelector('#notes-filter-reset');
    if (reset) {
        reset.onclick = async () => {
            _filters = { category: '', tag: '' };
            _page = 1;
            await loadAndRender();
        };
    }

    const tagInput = _container.querySelector('#notes-filter-tag');
    if (tagInput) {
        let timer = null;
        tagInput.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                _filters.tag = tagInput.value.trim();
                _page = 1;
                await loadAndRender();
            }, 250);
        });
    }

    _container.querySelectorAll('.notes-tag-chip').forEach((chip) => {
        chip.onclick = async () => {
            _filters.tag = chip.dataset.tag || '';
            _page = 1;
            await loadAndRender();
        };
    });

    _container.querySelectorAll('.note-card').forEach((card) => {
        card.onclick = () => {
            const note = _items.find((item) => String(item.id) === String(card.dataset.id));
            if (note) openNoteViewModal(note);
        };
    });
}

export function render(container) {
    _container = container;
    _items = [];
    _overview = null;
    _total = 0;
    _page = 1;
    _loading = false;
    _filters = { category: '', tag: '' };
    renderPage();
    loadAndRender();
    _dataChangedHandler = async (event) => {
        const changedType = event?.detail?.type;
        if (changedType && changedType !== 'note') return;
        await loadAndRender();
    };
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
    _items = [];
    _overview = null;
    _total = 0;
}

export function onRouteEnter(_params) {}
