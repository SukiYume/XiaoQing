import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { captureFormDraft, reopenWithDraft } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import {
    errorMessage,
    finiteNumber,
    noteCadenceSubtitle,
    nonNegativeInteger,
    previewText,
    textValue,
} from '../utils/format.js';
import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, todayRangeKey } from '../utils/date_ranges.js';
import { formatZonedDateTime, formatZonedMonthDay } from '../utils/timezone.js';
import { escapeHtml, subscribeDataChanges } from '../utils/ui.js';

const PAGE_SIZE             = 18;
const RANGE_KEYS            = new Set(RANGE_PRESET_OPTIONS.map(({ key }) => key));
const RANGE_LABELS          = new Map(RANGE_PRESET_OPTIONS.map(({ key, label }) => [key, label]));
const CADENCE_GRANULARITIES = new Set(['day', 'week', 'month', 'year']);
const DEFAULT_FILTERS       = Object.freeze({
    range: 'year',
    customStart: '',
    customEnd: '',
    category: '',
    tag: '',
    keyword: '',
});
const REFERENCE_LABELS = Object.freeze({
    event: '日程',
    task: '待办',
    note: '笔记',
    diary: '日记',
    ledger: '账目',
    item: '条目',
});

const NOTE_FIELDS = [
    { name: 'title', label: '标题', type: 'text', required: true },
    { name: 'category', label: '分类', type: 'text', placeholder: '未分类' },
    { name: 'tags', label: '标签', type: 'text', placeholder: '逗号分隔，如：工作,阅读' },
    { name: 'related_items', label: '关联条目 ID', type: 'text', placeholder: '逗号分隔，可填日程/待办/笔记 ID' },
    { name: 'content', label: '内容', type: 'textarea', rows: 10 },
];

let _container              = null;
let _items                  = [];
let _overview               = null;
let _total                  = 0;
let _page                   = 1;
let _loading                = false;
let _unsubscribeDataChanges = null;
let _loadVersion            = 0;
let _filters                = { ...DEFAULT_FILTERS };
let _activeRange            = { start: '', end: '' };
const _pendingDeletes       = new Set();

// ---------------------------------------------------------------------------
// 数据边界：所有接口响应先归一化，再进入模板和交互逻辑
// ---------------------------------------------------------------------------

function unitInterval(value) {
    return Math.min(1, Math.max(0, finiteNumber(value)));
}

function uniqueTextList(value, { caseInsensitive = false } = {}) {
    const list = Array.isArray(value) ? value : [];
    const seen = new Set();
    return list
        .map((item) => textValue(item).trim())
        .filter(Boolean)
        .filter((item) => {
            const key = caseInsensitive ? item.toLocaleLowerCase() : item;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
}

function tagsToString(tags) {
    return uniqueTextList(tags, { caseInsensitive: true }).join(', ');
}

function tagsFromInput(value) {
    return uniqueTextList(textValue(value).split(/[,，]/), { caseInsensitive: true });
}

function idListFromInput(value) {
    const seen = new Set();
    return textValue(value)
        .split(/[,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean)
        .filter((item) => {
            if (seen.has(item)) return false;
            seen.add(item);
            return true;
        });
}

function normalizeReference(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const id = textValue(value.id).trim();
    if (!id) return null;
    return {
        id,
        display_id: textValue(value.display_id, id).trim() || id,
        kind: textValue(value.kind, 'item').trim() || 'item',
        type: textValue(value.type).trim(),
        title: textValue(value.title).trim(),
    };
}

function normalizeReferences(value) {
    const seen = new Set();
    return (Array.isArray(value) ? value : []).map(normalizeReference).filter((reference) => {
        if (!reference || seen.has(reference.id)) return false;
        seen.add(reference.id);
        return true;
    });
}

function normalizeNote(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const id = textValue(value.id).trim();
    if (!id) return null;
    return {
        id,
        version: value.version,
        title: textValue(value.title).trim(),
        category: textValue(value.category).trim(),
        tags: uniqueTextList(value.tags, { caseInsensitive: true }),
        content: textValue(value.content),
        references: normalizeReferences(value.references),
        related_items: uniqueTextList(value.related_items),
        created_at: textValue(value.created_at).trim(),
        updated_at: textValue(value.updated_at).trim(),
    };
}

function normalizeOverview(value) {
    const source     = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const rawSummary =         source.summary && typeof source.summary === 'object' && !Array.isArray(source.summary) ? source.summary : {};
    const categories = (Array.isArray(source.categories) ? source.categories : [])
        .map((item) => ({
            category: textValue(item?.category).trim(),
            count: nonNegativeInteger(item?.count),
            share: unitInterval(item?.share),
        }))
        .filter(({ category }) => category);
    const hotTags = (Array.isArray(source.hot_tags) ? source.hot_tags : [])
        .map((item) => ({
            tag: textValue(item?.tag).trim(),
            count: nonNegativeInteger(item?.count),
        }))
        .filter(({ tag }) => tag);
    const cadence = (Array.isArray(source.cadence) ? source.cadence : [])
        .map((item) => ({
            label: textValue(item?.label || item?.date).trim(),
            count: nonNegativeInteger(item?.count),
        }))
        .filter(({ label }) => label);
    const granularity = textValue(source.cadence_granularity).trim();
    return {
        summary: {
            total_count: nonNegativeInteger(rawSummary.total_count),
            week_new_count: nonNegativeInteger(rawSummary.week_new_count),
            average_length: Math.max(0, finiteNumber(rawSummary.average_length)),
            tagged_rate: unitInterval(rawSummary.tagged_rate),
        },
        categories,
        hot_tags: hotTags,
        cadence,
        cadence_granularity: CADENCE_GRANULARITIES.has(granularity) ? granularity : 'day',
        all_categories: uniqueTextList(
            [
                ...(Array.isArray(source.all_categories) ? source.all_categories : []),
                ...categories.map(({ category }) => category),
            ],
            { caseInsensitive: true },
        ),
    };
}

function refsToString(note) {
    const values = [
        ...(Array.isArray(note?.references) ? note.references : []).map((ref) => ref?.id),
        ...(Array.isArray(note?.related_items) ? note.related_items : []),
    ];
    return idListFromInput(values.join(',')).join(', ');
}

function noteWordCount(note) {
    return Array.from(note.content.trim()).length;
}

// 只支持页面需要的 Markdown 子集。先转义原文，再添加受控标签，避免正文注入 HTML。
function renderInlineMarkdown(text) {
    let html = escapeHtml(textValue(text));
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    );
    return html;
}

function renderMarkdown(content) {
    const lines     = textValue(content).replace(/\r\n?/g, '\n').split('\n');
    const html      = [];
    let listOpen    = false;
    let codeOpen    = false;
    const closeList = () => {
        if (listOpen) {
            html.push('</ul>');
            listOpen = false;
        }
    };

    lines.forEach((line) => {
        if (/^```/.test(line.trim())) {
            closeList();
            if (codeOpen) {
                html.push('</code></pre>');
                codeOpen = false;
            } else {
                html.push('<pre><code>');
                codeOpen = true;
            }
            return;
        }
        if (codeOpen) {
            html.push(`${escapeHtml(line)}\n`);
            return;
        }
        if (!line.trim()) {
            closeList();
            return;
        }
        const heading = line.match(/^(#{1,3})\s+(.+)$/);
        if (heading) {
            closeList();
            const level = heading[1].length + 2;
            html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
            return;
        }
        const bullet = line.match(/^\s*[-*]\s+(.+)$/);
        if (bullet) {
            if (!listOpen) {
                html.push('<ul>');
                listOpen = true;
            }
            html.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
            return;
        }
        const quote = line.match(/^\s*>\s?(.+)$/);
        if (quote) {
            closeList();
            html.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
            return;
        }
        closeList();
        html.push(`<p>${renderInlineMarkdown(line)}</p>`);
    });
    closeList();
    if (codeOpen) html.push('</code></pre>');
    return html.join('');
}

function noteReferences(note) {
    const refs     = normalizeReferences(note?.references);
    const existing = new Set(refs.map((ref) => String(ref?.id || '')).filter(Boolean));
    uniqueTextList(note?.related_items).forEach((id) => {
        const refId = textValue(id).trim();
        if (refId && !existing.has(refId)) {
            existing.add(refId);
            refs.push({ kind: 'item', id: refId });
        }
    });
    return refs.filter((ref) => ref?.id);
}

function renderNoteReferences(note) {
    const refs = noteReferences(note);
    if (!refs.length) return '';
    return `
        <section class="note-reference-panel">
            <h4>关联条目</h4>
            <div class="note-reference-list">
                ${refs
                    .map((ref) => {
                        const type  = ref.type || ref.kind || 'item';
                        const label = REFERENCE_LABELS[type] || REFERENCE_LABELS[ref.kind] || '条目';
                        const title = ref.title || ref.id;
                        return `<div class="note-reference-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(title)}</strong><code>${escapeHtml(ref.display_id || ref.id)}</code></div>`;
                    })
                    .join('')}
            </div>
        </section>
    `;
}

function categoryOptions() {
    const categories = _overview?.all_categories || [];
    return [{ value: '', label: '全部分类' }, ...categories.map((category) => ({ value: category, label: category }))];
}

function deriveRangeDates() {
    return derivePresetRange(_filters.range, {
        today: todayRangeKey(),
        customStart: _filters.customStart,
        customEnd: _filters.customEnd,
        customFallback: 'month',
    });
}

function rangeLabel() {
    return RANGE_LABELS.get(_filters.range) || '当前范围';
}

async function resolveActiveRange() {
    const range = deriveRangeDates();
    if (_filters.range !== 'all') return range;
    return fetchItemRangeBounds(api, {
        type: 'note',
        filters: { ...sharedFilterParams(), ...(_filters.keyword ? { keyword: _filters.keyword } : {}) },
        fallbackEnd: range.end,
    });
}

function overviewReferenceDay(range) {
    const today = todayRangeKey();
    if (range?.start && range?.end && range.start <= today && today <= range.end) {
        return today;
    }
    return range?.end || today;
}

function dateTimeRangeForQuery(range) {
    if (!range?.start || !range?.end) return { start: '', end: '' };
    return {
        start: `${range.start}T00:00:00`,
        end: `${range.end}T23:59:59`,
    };
}

function sharedFilterParams() {
    const params = {};
    if (_filters.category) params.category = _filters.category;
    if (_filters.tag) params.tags = _filters.tag;
    return params;
}

function visualLevel(value, maximum = 1) {
    if (maximum <= 0) return 0;
    return Math.min(10, Math.max(0, Math.round((finiteNumber(value) / maximum) * 10)));
}

async function fetchOverview(range = _activeRange) {
    const params = {
        today: overviewReferenceDay(range),
        start_date: range?.start || '',
        end_date: range?.end || '',
        ...sharedFilterParams(),
    };
    const res = await api.get('/stats/notes/overview', params);
    return normalizeOverview(res?.data);
}

async function fetchNotes(page = 1, range = _activeRange) {
    const params = {
        type: 'note',
        date_field: 'created_at',
        page: Math.max(1, nonNegativeInteger(page)),
        page_size: PAGE_SIZE,
        sort: 'updated_at',
        order: 'desc',
        ...sharedFilterParams(),
    };
    const dateRange = dateTimeRangeForQuery(range);
    if (dateRange.start && dateRange.end) {
        params.start_date = dateRange.start;
        params.end_date = dateRange.end;
    }
    if (_filters.keyword) params.keyword = _filters.keyword;
    const res = await api.get('/items', params);
    const items = (Array.isArray(res?.data?.items) ? res.data.items : []).map(normalizeNote).filter(Boolean);
    return {
        items,
        total: Math.max(items.length, nonNegativeInteger(res?.data?.total)),
    };
}

// ---------------------------------------------------------------------------
// 页面渲染：模板只接收已经归一化的数据，动态文本一律转义
// ---------------------------------------------------------------------------

function renderHero() {
    const summary = _overview?.summary || {};
    return `
        <section class="notes-hero pendo-page-header">
            <div>
                <span class="eyebrow"><i></i>NOTEBOOK</span>
                <h2>笔记</h2>
                <p>${escapeHtml(rangeLabel())} ${summary.total_count || 0} 条笔记，集中整理灵感、摘录和草稿。</p>
            </div>
            <div class="notes-hero-actions">
                <button class="btn btn-primary" id="notes-add-top" type="button">＋ 新建笔记</button>
            </div>
        </section>
    `;
}

function renderSummary() {
    const summary    = _overview?.summary || {};
    const taggedRate = Math.round((summary.tagged_rate || 0) * 100);
    return `
        <section class="notes-summary-grid">
            <div class="notes-summary-card">
                <div class="notes-summary-label">${escapeHtml(rangeLabel())}笔记数</div>
                <div class="notes-summary-value">${summary.total_count || 0}</div>
                <div class="notes-summary-meta">当前时间范围内的累计条目</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">近 7 天新增</div>
                <div class="notes-summary-value">${summary.week_new_count || 0}</div>
                <div class="notes-summary-meta">以当前范围结束日为参照计算</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">平均字数</div>
                <div class="notes-summary-value">${Math.round(summary.average_length || 0)}</div>
                <div class="notes-summary-meta">${escapeHtml(rangeLabel())}内笔记概况</div>
            </div>
            <div class="notes-summary-card">
                <div class="notes-summary-label">已打标签</div>
                <div class="notes-summary-value">${taggedRate}%</div>
                <div class="notes-summary-meta">${escapeHtml(rangeLabel())}内便于后续回看与筛选</div>
            </div>
        </section>
    `;
}

function renderRangeControls() {
    const showCustom = _filters.range === 'custom';
    const rangeText  =         _activeRange.start && _activeRange.end ? `${_activeRange.start} → ${_activeRange.end}` : '当前范围';
    return `
        <section class="notes-range-panel">
            <div class="notes-range-row">
                ${RANGE_PRESET_OPTIONS.map(
                    (item) => `
                    <button class="notes-range-btn ${_filters.range === item.key ? 'active' : ''}" type="button"
                            data-range="${escapeHtml(item.key)}" aria-pressed="${_filters.range === item.key}">
                        ${escapeHtml(item.label)}
                    </button>
                `,
                ).join('')}
            </div>
            ${
                showCustom
                    ? `
                <div class="notes-custom-range">
                    <input class="notes-date-field" id="notes-range-start" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_filters.customStart)}" aria-label="笔记范围开始日期">
                    <span class="notes-range-sep">至</span>
                    <input class="notes-date-field" id="notes-range-end" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_filters.customEnd)}" aria-label="笔记范围结束日期">
                    <button class="btn btn-secondary" id="notes-range-apply" type="button">应用</button>
                </div>
            `
                    : ''
            }
            <div class="notes-range-meta">${escapeHtml(rangeText)}</div>
        </section>
    `;
}

function renderCadencePanel() {
    const cadence     = _overview?.cadence || [];
    const maxCount    = Math.max(1, ...cadence.map((item) => item.count || 0));
    const granularity = _overview?.cadence_granularity || 'day';
    return `
        <section class="notes-panel notes-cadence-panel">
            <div class="notes-panel-head">
                <div>
                    <h3>书写节奏</h3>
                    <p>${escapeHtml(noteCadenceSubtitle(granularity, rangeLabel()))}</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-meter">
                    ${cadence
                        .map((item) => {
                            const level = visualLevel(item.count, maxCount);
                            const label = `${item.label}：${item.count} 条`;
                            return `
                        <div class="notes-meter-col">
                            <div class="notes-meter-value">${item.count}</div>
                            <div class="notes-meter-track" role="img" aria-label="${escapeHtml(label)}">
                                <div class="notes-meter-stick notes-level-${level}"></div>
                            </div>
                            <div class="notes-meter-label">${escapeHtml(item.label)}</div>
                        </div>`;
                        })
                        .join('')}
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
                    <p>查看${escapeHtml(rangeLabel())}内笔记主要集中在哪些主题。</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-category-list">
                    ${categories
                        .map((item) => {
                            const percent = Math.round(item.share * 100);
                            return `
                        <div class="notes-category-row">
                            <div>
                                <div class="notes-category-top">
                                    <span class="notes-category-name">${escapeHtml(item.category)}</span>
                                    <span class="notes-category-count">${item.count} 条</span>
                                </div>
                                <div class="notes-category-track">
                                    <div class="notes-category-fill notes-level-${visualLevel(item.share)}"
                                         role="img" aria-label="${escapeHtml(`${item.category}：${percent}%`)}"></div>
                                </div>
                            </div>
                            <div class="note-tag">${percent}%</div>
                        </div>`;
                        })
                        .join('')}
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
                    <p>点击标签可直接筛选当前范围内的笔记。</p>
                </div>
            </div>
            <div class="notes-panel-body">
                <div class="notes-tag-list">
                    ${tags
                        .map(
                            (item) => `
                        <button class="notes-tag-chip" data-tag="${escapeHtml(item.tag)}" type="button">
                            <span>#${escapeHtml(item.tag)}</span>
                            <span>${item.count}</span>
                        </button>`,
                        )
                        .join('')}
                </div>
            </div>
        </section>
    `;
}

function renderFilters() {
    return `
        <section class="notes-filter-bar">
            <div class="notes-filter-field">
                <label for="notes-filter-keyword">关键词</label>
                <input id="notes-filter-keyword" type="search" placeholder="搜索标题、正文、分类或标签" value="${escapeHtml(_filters.keyword)}">
            </div>
            <div class="notes-filter-field">
                <label for="notes-filter-tag">标签筛选</label>
                <input id="notes-filter-tag" type="text" placeholder="输入标签，例如：阅读" value="${escapeHtml(_filters.tag)}">
            </div>
            <div class="notes-filter-field">
                <label id="notes-filter-category-label">分类</label>
                ${renderCustomSelect({
                    id: 'notes-filter-category',
                    options: categoryOptions(),
                    selected: _filters.category,
                    className: 'pselect-block pselect-theme-notes',
                    placeholder: '全部分类',
                    labelledBy: 'notes-filter-category-label',
                })}
            </div>
            <div class="notes-filter-actions">
                <button class="btn btn-secondary" id="notes-filter-reset" type="button">重置筛选</button>
            </div>
        </section>
    `;
}

function renderNoteTags(tags, limit = 4, emptyLabel = '未打标签') {
    if (!tags.length) return `<span class="note-tag note-tag-muted">${escapeHtml(emptyLabel)}</span>`;
    return tags
        .slice(0, limit)
        .map((tag) => `<span class="note-tag">#${escapeHtml(tag)}</span>`)
        .join('');
}

function renderSpotlight(note) {
    const tags           = uniqueTextList(note.tags, { caseInsensitive: true });
    const preview        = previewText(note.content, 220);
    const spotlightLabel = _page === 1 ? '最新更新' : '本页首条';
    const title          = note.title || '(无标题)';
    return `
        <button class="note-card notes-spotlight" type="button" data-open-note="${escapeHtml(note.id)}"
                aria-label="${escapeHtml(`打开笔记：${title}`)}">
            <span class="notes-spotlight-main">
                <span class="notes-spotlight-kicker">
                    <span class="notes-spotlight-label">${spotlightLabel}</span>
                    <span class="note-card-category">${escapeHtml(note.category || '未分类')}</span>
                </span>
                <span class="notes-spotlight-title">${escapeHtml(title)}</span>
                <span class="notes-spotlight-preview">${preview ? escapeHtml(preview) : '<span class="note-muted">（无内容）</span>'}</span>
                <span class="notes-spotlight-tags">${renderNoteTags(tags, 5)}</span>
            </span>
            <span class="notes-spotlight-side">
                <span class="notes-spotlight-stat">
                    <span class="notes-spotlight-stat-label">更新日期</span>
                    <span class="notes-spotlight-stat-value">${escapeHtml(formatZonedMonthDay(note.updated_at || note.created_at))}</span>
                </span>
                <span class="notes-spotlight-stat">
                    <span class="notes-spotlight-stat-label">内容长度</span>
                    <span class="notes-spotlight-stat-value">${noteWordCount(note)} 字</span>
                </span>
            </span>
        </button>
    `;
}

function renderNoteRow(note, index) {
    const tags      = uniqueTextList(note.tags, { caseInsensitive: true });
    const preview   = previewText(note.content, 96);
    const date      = formatZonedMonthDay(note.updated_at || note.created_at);
    const wordCount = noteWordCount(note);
    const title     = note.title || '(无标题)';
    return `
        <button class="note-card note-row" type="button" data-open-note="${escapeHtml(note.id)}"
                aria-label="${escapeHtml(`打开笔记：${title}`)}">
            <span class="note-row-main">
                <span class="note-row-top">
                    <span class="note-row-title">${escapeHtml(title)}</span>
                    <span class="note-card-category note-row-cat-desktop">${escapeHtml(note.category || '未分类')}</span>
                </span>
                <span class="note-row-preview">${preview ? escapeHtml(preview) : '<span class="note-muted">（无内容）</span>'}</span>
                <span class="note-row-tags">${renderNoteTags(tags, 3)}</span>
                <span class="note-row-footer">
                    <span class="note-card-category">${escapeHtml(note.category || '未分类')}</span>
                    ${renderNoteTags(tags, 3)}
                    <span class="note-row-footer-sep">·</span>
                    <span class="note-row-footer-meta">${escapeHtml(date)}</span>
                    <span class="note-row-footer-meta">${wordCount} 字</span>
                </span>
            </span>
            <span class="note-row-side">
                <span class="note-row-order">${String(index + 1).padStart(2, '0')}</span>
                <span class="note-row-meta">
                    <span>${escapeHtml(date)}</span>
                    <span>${wordCount} 字</span>
                </span>
            </span>
        </button>
    `;
}

function renderWorkspaceCollection() {
    if (!_items.length) {
        return `<div class="notes-empty writing-empty">
            <img src="assets/note-library.webp" width="448" height="252" loading="lazy" alt="" />
            <div class="writing-prompt"><strong>小青的摘录夹</strong><p>${_filters.keyword || _filters.tag || _filters.category
                ? '当前筛选下有 0 条笔记。可以重置筛选，看看其他笔记。'
                : '这段时间的摘录夹还是空的。翻到想留的一句话，就记在这里。'}</p></div>
        </div>`;
    }
    const [spotlight, ...rest] = _items;
    return `
        <div class="notes-collection">
            ${spotlight ? renderSpotlight(spotlight) : ''}
            ${
                rest.length
                    ? `
                <div class="notes-list-shell">
                    <div class="notes-list-head">
                        <div>
                            <h4 class="notes-list-title">继续浏览</h4>
                            <p class="notes-list-subtitle">按更新时间查看其余笔记。</p>
                        </div>
                        <div class="note-tag">${rest.length} 条</div>
                    </div>
                    <div class="notes-list">${rest.map((note, index) => renderNoteRow(note, index + (_page - 1) * PAGE_SIZE + 1)).join('')}</div>
                </div>`
                    : ''
            }
        </div>
    `;
}

function renderWorkspace() {
    const subtitle = _page === 1 ? '先看最新更新，再快速扫读其余笔记。' : '当前页继续按更新时间浏览笔记。';
    return `
        <section class="notes-workspace">
            <div class="notes-workspace-head">
                <div>
                    <h3 class="notes-workspace-title">最近笔记</h3>
                    <p class="notes-workspace-subtitle">${subtitle}</p>
                </div>
                <div class="note-tag">${nonNegativeInteger(_total)} 条匹配</div>
            </div>
            <div class="notes-workspace-body">
                ${renderWorkspaceCollection()}
                <div id="notes-pagination" class="notes-pagination"></div>
            </div>
        </section>
    `;
}

function renderPage() {
    if (!_container) return;

    if (_loading && !_overview) {
        _container.innerHTML = `
            <div class="notes-shell" aria-busy="true">
                <div class="notes-empty" role="status">正在加载笔记空间...</div>
            </div>
        `;
        return;
    }

    _container.innerHTML = `
        <div class="notes-shell">
            ${renderHero()}
            <div class="notes-stack">
                ${renderRangeControls()}
                ${renderFilters()}
                ${renderWorkspace()}
                <details class="pendo-analysis"><summary>查看笔记分析</summary>
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
                </details>
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
                const nextPage = Math.max(1, nonNegativeInteger(page));
                if (nextPage === _page) return;
                _page = nextPage;
                await loadAndRender();
            },
        });
    }

    attachListeners();
}

async function loadAndRender() {
    const root = _container;
    if (!root) return;
    const version       = ++_loadVersion;
    const requestedPage = _page;
    _loading = true;
    renderPage();
    try {
        const activeRange = await resolveActiveRange();
        if (_container !== root || version !== _loadVersion) return;
        const [overview, initialList] = await Promise.all([
            fetchOverview(activeRange),
            fetchNotes(requestedPage, activeRange),
        ]);
        if (_container !== root || version !== _loadVersion) return;

        let list      = initialList;
        const maxPage = Math.max(1, Math.ceil(list.total / PAGE_SIZE));
        if (requestedPage > maxPage) {
            _page = maxPage;
            list = await fetchNotes(maxPage, activeRange);
            if (_container !== root || version !== _loadVersion) return;
        } else {
            _page = requestedPage;
        }
        _activeRange = activeRange;
        _overview = overview;
        _items = list.items;
        _total = list.total;
    } catch (err) {
        if (_container !== root || version !== _loadVersion) return;
        _activeRange = deriveRangeDates();
        _overview = null;
        _items = [];
        _total = 0;
        showToast(`加载笔记失败：${errorMessage(err)}`, 'error');
    } finally {
        if (_container === root && version === _loadVersion) {
            _loading = false;
            renderPage();
        }
    }
}

// ---------------------------------------------------------------------------
// 详情与编辑：删除逻辑只有一个入口，所有变更只广播一次失效事件
// ---------------------------------------------------------------------------

function dispatchNoteChange() {
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'note' } }));
}

async function confirmAndDeleteNote(note, cancelText, reopen) {
    if (_pendingDeletes.has(note.id)) return false;
    _pendingDeletes.add(note.id);
    try {
        const confirmed = await showConfirmModal({
            title: '删除笔记',
            message: `确定要删除“${note.title || '这条笔记'}”吗？删除后内容将无法恢复。`,
            confirmText: '删除',
            cancelText,
            tone: 'danger',
        });
        if (!confirmed) {
            reopen?.();
            return false;
        }
        await api.delete(`/items/${encodeURIComponent(note.id)}`);
        showToast('笔记已删除', 'success');
        dispatchNoteChange();
        return true;
    } catch (error) {
        showToast(`删除失败：${errorMessage(error)}`, 'error');
        reopen?.();
        return false;
    } finally {
        _pendingDeletes.delete(note.id);
    }
}

export function openNoteViewModal(rawNote) {
    const note = normalizeNote(rawNote);
    if (!note) {
        showToast('无法打开无效笔记', 'warning');
        return null;
    }
    const tags     = note.tags;
    const bodyHTML = `
        <div class="note-view-meta">
            <span class="note-card-category">${escapeHtml(note.category || '未分类')}</span>
            ${tags.map((tag) => `<span class="note-tag">#${escapeHtml(tag)}</span>`).join('')}
            <span class="note-view-secondary">更新于 ${escapeHtml(formatZonedDateTime(note.updated_at || note.created_at))}</span>
        </div>
        <div class="note-view-content">${renderMarkdown(note.content || '')}</div>
        ${renderNoteReferences(note)}
    `;
    const footer = `
        <button class="btn btn-danger btn-sm note-modal-delete" id="note-delete" type="button">删除</button>
        <button class="btn btn-secondary" id="note-close" type="button">关闭</button>
        <button class="btn btn-primary" id="note-edit" type="button">编辑</button>
    `;
    const content = showModal(note.title || '(无标题)', safeHtml(bodyHTML), {
        footer: safeHtml(footer),
    });
    const closeButton  = content.querySelector('#note-close');
    const editButton   = content.querySelector('#note-edit');
    const deleteButton = content.querySelector('#note-delete');
    closeButton.onclick = closeModal;
    editButton.onclick = () => {
        closeModal(content);
        openNoteFormModal(note);
    };
    deleteButton.onclick = async () => {
        if (deleteButton.disabled) return;
        deleteButton.disabled = true;
        closeModal(content);
        await confirmAndDeleteNote(note, '返回详情', () => openNoteViewModal(note));
    };
    return content;
}

function openNoteFormModal(existing = null) {
    const note = existing ? normalizeNote(existing) : null;
    if (existing && !note) {
        showToast('无法编辑无效笔记', 'warning');
        return null;
    }
    const isEdit = Boolean(note);
    const fields = NOTE_FIELDS.map((field) => {
        let value = '';
        if (note) {
            if (field.name === 'tags') value = tagsToString(note.tags);
            else if (field.name === 'related_items') value = refsToString(note);
            else value = note[field.name] ?? '';
        }
        return { ...field, value };
    });

    const content = showModal(
        isEdit ? '编辑笔记' : '新建笔记',
        safeHtml(`<form id="note-form">${buildFormHTML(fields)}</form>`),
        {
            footer: safeHtml(`
                ${isEdit ? '<button class="btn btn-danger btn-sm note-modal-delete" id="note-modal-delete" type="button">删除</button>' : ''}
                <button class="btn btn-secondary" id="note-modal-cancel" type="button">取消</button>
                <button class="btn btn-primary" id="note-modal-save" type="button">保存</button>
            `),
        },
    );

    initFormInteractions(content);
    const form       = content.querySelector('#note-form');
    const saveButton = content.querySelector('#note-modal-save');
    content.querySelector('#note-modal-cancel').onclick = closeModal;

    if (note) {
        const deleteButton = content.querySelector('#note-modal-delete');
        deleteButton.onclick = async () => {
            if (deleteButton.disabled) return;
            deleteButton.disabled = true;
            // 删除确认会替换编辑弹窗：先记下草稿，取消或删除失败时带着草稿回到编辑。
            const draft = captureFormDraft(form);
            closeModal(content);
            await confirmAndDeleteNote(note, '返回编辑', () =>
                reopenWithDraft(() => openNoteFormModal(note), draft, '#note-form'));
        };
    }

    let saving     = false;
    const saveNote = async (event) => {
        event?.preventDefault?.();
        if (saving) return;
        const data  = getFormData(form) || {};
        const title = textValue(data.title).trim();
        if (!title) {
            showToast('请填写标题', 'warning');
            return;
        }
        const relatedItems = idListFromInput(data.related_items);
        const payload      = {
            title,
            category: textValue(data.category).trim(),
            tags: tagsFromInput(data.tags),
            related_items: relatedItems,
            references: relatedItems.map((id) => ({ kind: 'item', id })),
            content: textValue(data.content),
        };
        saving = true;
        saveButton.disabled = true;
        try {
            if (note) {
                await api.put(`/items/${encodeURIComponent(note.id)}`, { ...payload, version: note.version });
                showToast('笔记已更新', 'success');
            } else {
                await api.post('/items', { type: 'note', ...payload });
                showToast('笔记已创建', 'success');
            }
            closeModal(content);
            dispatchNoteChange();
        } catch (error) {
            showToast(`保存失败：${errorMessage(error)}`, 'error');
        } finally {
            saving = false;
            saveButton.disabled = false;
        }
    };
    form.onsubmit = saveNote;
    saveButton.onclick = saveNote;
    return content;
}

function attachListeners() {
    const root = _container;
    if (!root) return;

    initCustomSelects(root, {
        'notes-filter-category': async (value) => {
            const category = textValue(value).trim();
            if (category === _filters.category) return;
            _filters.category = category;
            _page = 1;
            await loadAndRender();
        },
    });

    root.querySelectorAll('.notes-range-btn').forEach((button) => {
        button.onclick = async () => {
            const requestedRange = textValue(button.dataset.range).trim();
            const nextRange      = RANGE_KEYS.has(requestedRange) ? requestedRange : 'month';
            if (nextRange === _filters.range) return;
            _filters.range = nextRange;
            if (nextRange === 'custom' && (!_filters.customStart || !_filters.customEnd)) {
                const fallback = derivePresetRange('month', { today: todayRangeKey() });
                _filters.customStart = fallback.start;
                _filters.customEnd = fallback.end;
            }
            _page = 1;
            await loadAndRender();
        };
    });

    const applyCustomRange = async () => {
        if (_container !== root) return;
        const start     = textValue(root.querySelector('#notes-range-start')?.value).trim();
        const end       = textValue(root.querySelector('#notes-range-end')?.value).trim();
        const candidate = derivePresetRange('custom', {
            today: todayRangeKey(),
            customStart: start,
            customEnd: end,
            customFallback: '',
        });
        if (candidate.start !== start || candidate.end !== end) {
            showToast('请输入有效日期，格式为 YYYY-MM-DD', 'warning');
            return;
        }
        if (start > end) {
            showToast('开始日期不能晚于结束日期', 'warning');
            return;
        }
        if (start === _filters.customStart && end === _filters.customEnd) return;
        _filters.customStart = start;
        _filters.customEnd = end;
        _page = 1;
        await loadAndRender();
    };

    const rangeApply = root.querySelector('#notes-range-apply');
    if (rangeApply) {
        rangeApply.onclick = applyCustomRange;
        ['#notes-range-start', '#notes-range-end'].forEach((selector) => {
            root.querySelector(selector)?.addEventListener('keydown', async (event) => {
                if (event.key !== 'Enter' || event.isComposing) return;
                event.preventDefault();
                await applyCustomRange();
            });
        });
    }

    const addTop = root.querySelector('#notes-add-top');
    if (addTop) addTop.onclick = () => openNoteFormModal(null);

    const reset = root.querySelector('#notes-filter-reset');
    if (reset) {
        reset.onclick = async () => {
            if (!_filters.category && !_filters.tag && !_filters.keyword) return;
            _filters.category = '';
            _filters.tag = '';
            _filters.keyword = '';
            _page = 1;
            await loadAndRender();
        };
    }

    // 关键词与标签输入共享提交规则，避免 Enter 后 blur 再触发一次重复请求。
    const bindTextFilter = (selector, key) => {
        const input = root.querySelector(selector);
        if (!input) return;
        const commit = async (value) => {
            const nextValue = textValue(value).trim();
            if (_filters[key] === nextValue) return;
            _filters[key] = nextValue;
            _page = 1;
            await loadAndRender();
        };
        input.addEventListener('change', () => commit(input.value));
        input.addEventListener('keydown', async (event) => {
            if (event.key === 'Enter' && !event.isComposing) {
                event.preventDefault();
                await commit(input.value);
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                input.value = '';
                await commit('');
            }
        });
    };
    bindTextFilter('#notes-filter-keyword', 'keyword');
    bindTextFilter('#notes-filter-tag', 'tag');

    root.querySelectorAll('.notes-tag-chip').forEach((chip) => {
        chip.onclick = async () => {
            const tag = textValue(chip.dataset.tag).trim();
            if (tag === _filters.tag) return;
            _filters.tag = tag;
            _page = 1;
            await loadAndRender();
        };
    });

    root.querySelectorAll('[data-open-note]').forEach((card) => {
        card.onclick = () => {
            const note = _items.find((item) => item.id === textValue(card.dataset.openNote));
            if (note) openNoteViewModal(note);
        };
    });
}

export async function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('笔记页需要有效的容器元素');
    }
    _loadVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = container;
    _items = [];
    _overview = null;
    _total = 0;
    _page = 1;
    _loading = false;
    _filters = { ...DEFAULT_FILTERS };
    _activeRange = deriveRangeDates();
    _unsubscribeDataChanges = subscribeDataChanges('note', loadAndRender);
    await loadAndRender();
}

export function destroy() {
    _loadVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
    _items = [];
    _overview = null;
    _total = 0;
    _page = 1;
    _loading = false;
    _filters = { ...DEFAULT_FILTERS };
    _activeRange = { start: '', end: '' };
}
