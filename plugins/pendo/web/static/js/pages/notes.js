import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
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
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, subscribeDataChanges } from '../utils/ui.js';

const PAGE_SIZE = 18;
const CSS_ID = 'pendo-notes-styles';
const RANGE_KEYS = new Set(RANGE_PRESET_OPTIONS.map(({ key }) => key));
const RANGE_LABELS = new Map(RANGE_PRESET_OPTIONS.map(({ key, label }) => [key, label]));
const CADENCE_GRANULARITIES = new Set(['day', 'week', 'month', 'year']);
const DEFAULT_FILTERS = Object.freeze({
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

let _container = null;
let _items = [];
let _overview = null;
let _total = 0;
let _page = 1;
let _loading = false;
let _unsubscribeDataChanges = null;
let _loadVersion = 0;
let _filters = { ...DEFAULT_FILTERS };
let _activeRange = { start: '', end: '' };
const _pendingDeletes = new Set();

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
    const source = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const rawSummary =
        source.summary && typeof source.summary === 'object' && !Array.isArray(source.summary) ? source.summary : {};
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
    return textValue(note?.content).trim().length;
}

// 只支持页面需要的 Markdown 子集；先转义原文，再添加受控标签，避免正文注入 HTML。
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
    const lines = textValue(content).replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let listOpen = false;
    let codeOpen = false;
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
    const refs = normalizeReferences(note?.references);
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
                        const type = ref.type || ref.kind || 'item';
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
        sortField: 'created_at',
        startField: 'created_at',
        endField: 'created_at',
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

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('notes-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}

        /* 页面头部、日期范围与摘要 */
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
            white-space: nowrap; line-height: 1; flex-shrink: 0;
        }
        .notes-hero-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
        .notes-stack { display: flex; flex-direction: column; gap: 18px; }
        .notes-range-panel {
            display: flex; flex-direction: column; gap: 12px; padding: 16px 18px;
            border-radius: 22px; background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.94));
            border: 1px solid rgba(59,130,246,0.12); box-shadow: 0 14px 30px rgba(37,99,235,0.04);
        }
        .notes-range-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .notes-range-btn {
            height: 40px; padding: 0 18px; border-radius: 999px; border: 1px solid rgba(203,213,225,0.92);
            background: rgba(255,255,255,0.92); color: #334155; font-size: 13px; font-weight: 800; cursor: pointer;
            transition: background .16s ease, border-color .16s ease, transform .16s ease, color .16s ease;
        }
        .notes-range-btn:hover { transform: translateY(-1px); }
        .notes-range-btn.active {
            background: rgba(59,130,246,0.12); border-color: rgba(59,130,246,0.28); color: #1d4ed8;
        }
        .notes-range-btn:focus-visible,
        .notes-tag-chip:focus-visible,
        .note-card:focus-visible {
            outline: 3px solid rgba(59,130,246,0.26);
            outline-offset: 2px;
        }
        .notes-custom-range { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .notes-date-field {
            width: 100%; max-width: 220px; height: 40px; border-radius: 14px; border: 1px solid rgba(59,130,246,0.14);
            background: rgba(255,255,255,0.92); padding: 0 14px; font-size: 13px; color: var(--color-text);
        }
        .notes-date-field:focus {
            outline: none; border-color: var(--color-notes, #3B82F6); box-shadow: 0 0 0 3px rgba(59,130,246,0.12);
        }
        .notes-range-sep { font-size: 13px; font-weight: 800; color: var(--color-text-secondary); }
        .notes-range-meta { font-size: 12px; color: var(--color-text-secondary); font-weight: 700; }
        .notes-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
        .notes-summary-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.94));
            border: 1px solid rgba(59,130,246,0.12); border-radius: 22px; padding: 18px;
            box-shadow: 0 14px 30px rgba(37,99,235,0.05);
            min-width: 0;
        }
        .notes-summary-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .notes-summary-value {
            margin-top: 10px; font-size: clamp(24px, 1.9vw, 30px); font-weight: 820; line-height: 1.04; letter-spacing: -0.03em; color: #0f172a;
            overflow-wrap: anywhere; word-break: break-word;
        }
        .notes-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }

        /* 概览图表与筛选区 */
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
            display: grid; grid-template-columns: repeat(auto-fit, minmax(34px, 1fr)); gap: 8px; align-items: end;
        }
        .notes-meter-col { display: flex; flex-direction: column; align-items: center; gap: 4px; }
        .notes-meter-value { font-size: 10px; font-weight: 700; color: var(--color-text); }
        .notes-meter-track {
            display: flex; align-items: flex-end; width: 100%; height: 50px; overflow: hidden;
            border-radius: 999px 999px 10px 10px; background: rgba(191,219,254,0.34);
        }
        .notes-meter-stick {
            width: 100%; height: var(--notes-level, 0%); min-height: 0; border-radius: inherit;
            background: linear-gradient(180deg, rgba(59,130,246,0.9), rgba(14,165,233,0.46));
        }
        .notes-level-0 { --notes-level: 0%; }
        .notes-level-1 { --notes-level: 10%; }
        .notes-level-2 { --notes-level: 20%; }
        .notes-level-3 { --notes-level: 30%; }
        .notes-level-4 { --notes-level: 40%; }
        .notes-level-5 { --notes-level: 50%; }
        .notes-level-6 { --notes-level: 60%; }
        .notes-level-7 { --notes-level: 70%; }
        .notes-level-8 { --notes-level: 80%; }
        .notes-level-9 { --notes-level: 90%; }
        .notes-level-10 { --notes-level: 100%; }
        .notes-meter-label { font-size: 10px; color: var(--color-text-secondary); }
        .notes-cadence-panel .notes-panel-body { padding-top: 10px; padding-bottom: 16px; }
        .notes-category-list { display: flex; flex-direction: column; gap: 12px; }
        .notes-category-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }
        .notes-category-top { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; margin-bottom: 6px; }
        .notes-category-name { font-weight: 700; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .notes-category-count { color: var(--color-text-secondary); font-weight: 700; }
        .notes-category-track { height: 10px; border-radius: 999px; background: rgba(191,219,254,0.42); overflow: hidden; }
        .notes-category-fill {
            width: var(--notes-level, 0%); height: 100%; border-radius: inherit;
            background: linear-gradient(90deg, rgba(59,130,246,0.88), rgba(14,165,233,0.55));
        }
        .notes-tag-list { display: flex; flex-wrap: wrap; gap: 8px; }
        .notes-tag-chip {
            display: inline-flex; align-items: center; gap: 6px; padding: 8px 12px; border-radius: 999px;
            background: rgba(255,255,255,0.84); border: 1px solid rgba(59,130,246,0.12); color: #1d4ed8;
            font-size: 12px; font-weight: 700; cursor: pointer;
            white-space: nowrap; line-height: 1; flex-shrink: 0;
        }
        .notes-tag-chip:hover { background: rgba(219,234,254,0.8); }
        .notes-filter-bar {
            display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(220px, 0.46fr) auto; gap: 12px;
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

        /* 笔记工作区：焦点卡片与紧凑列表共用原生按钮交互 */
        .notes-workspace {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,246,255,0.95));
            border: 1px solid rgba(59,130,246,0.12); border-radius: 26px; box-shadow: 0 18px 34px rgba(37,99,235,0.05);
            overflow: hidden;
        }
        .notes-workspace-head { display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 18px 20px; border-bottom: 1px solid rgba(191,219,254,0.42); }
        .notes-workspace-head > div { min-width: 0; flex: 1 1 220px; }
        .notes-workspace-head > .note-tag { flex: 0 0 auto; margin-left: auto; }
        .notes-workspace-title { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); }
        .notes-workspace-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .notes-workspace-body { padding: 18px 20px 22px; display: flex; flex-direction: column; gap: 16px; }
        .notes-collection { display: flex; flex-direction: column; gap: 14px; }
        .note-card {
            width: 100%; border: 0; appearance: none; text-align: left; color: inherit; font: inherit;
            cursor: pointer;
            transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease, background .16s ease;
        }
        .note-card:hover { transform: translateY(-2px); border-color: rgba(59,130,246,0.24); box-shadow: 0 18px 32px rgba(37,99,235,0.08); }
        .notes-spotlight {
            display: grid; grid-template-columns: minmax(0, 1fr) minmax(148px, 0.28fr); gap: 18px;
            padding: 22px; border-radius: 22px; border: 1px solid rgba(59,130,246,0.14);
            background:
                radial-gradient(circle at top right, rgba(59,130,246,0.16), transparent 34%),
                linear-gradient(145deg, rgba(255,255,255,0.99), rgba(239,246,255,0.96));
            box-shadow: 0 16px 30px rgba(37,99,235,0.05);
            align-items: stretch;
        }
        .notes-spotlight-main { min-width: 0; display: flex; flex-direction: column; gap: 12px; }
        .notes-spotlight-kicker { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .notes-spotlight-label {
            display: inline-flex; align-items: center; height: 28px; padding: 0 12px; border-radius: 999px;
            background: rgba(29,78,216,0.10); color: #1d4ed8; font-size: 11px; font-weight: 800; letter-spacing: 0.04em;
            white-space: nowrap; line-height: 1; flex-shrink: 0;
        }
        .notes-spotlight-title { display: block; margin: 0; font-size: 24px; font-weight: 820; line-height: 1.24; letter-spacing: -0.03em; color: var(--color-text); }
        .notes-spotlight-preview {
            font-size: 14px; line-height: 1.8; color: var(--color-text-secondary); word-break: break-word;
            display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
        }
        .notes-spotlight-tags { display: flex; flex-wrap: wrap; gap: 8px; }
        .notes-spotlight-side {
            display: flex; flex-direction: column; justify-content: space-between; gap: 14px;
            padding-left: 18px; border-left: 1px solid rgba(191,219,254,0.56);
        }
        .notes-spotlight-stat {
            display: block;
            padding: 12px 14px; border-radius: 16px; background: rgba(255,255,255,0.82); border: 1px solid rgba(191,219,254,0.54);
        }
        .notes-spotlight-stat-label { font-size: 11px; font-weight: 800; color: var(--color-text-secondary); letter-spacing: 0.04em; text-transform: uppercase; }
        .notes-spotlight-stat-value { display: block; margin-top: 6px; font-size: 18px; font-weight: 760; color: var(--color-text); }
        .notes-list-shell {
            border: 1px solid rgba(59,130,246,0.10); border-radius: 20px; background: rgba(255,255,255,0.76);
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
            overflow: hidden;
        }
        .notes-list-head {
            display: flex; align-items: center; justify-content: space-between; gap: 12px;
            padding: 14px 16px; border-bottom: 1px solid rgba(191,219,254,0.42); background: rgba(248,250,252,0.85);
        }
        .notes-list-head > div { min-width: 0; }
        .notes-list-title { margin: 0; font-size: 14px; font-weight: 780; color: var(--color-text); }
        .notes-list-subtitle { margin: 4px 0 0; font-size: 12px; color: var(--color-text-secondary); }
        .notes-list { display: flex; flex-direction: column; }
        .note-row {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: flex-start;
            padding: 16px; border-bottom: 1px solid rgba(226,232,240,0.82); border-radius: 0;
            background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(248,250,252,0.78));
        }
        .note-row:last-child { border-bottom: none; }
        .note-row-main { min-width: 0; display: flex; flex-direction: column; gap: 8px; }
        .note-row-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
        .note-row-title { margin: 0; font-size: 17px; font-weight: 760; line-height: 1.42; color: var(--color-text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .note-row-preview {
            font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); word-break: break-word;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
        }
        .note-row-tags { display: flex; flex-wrap: wrap; gap: 6px; }
        .note-row-side {
            min-width: 96px; display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
            text-align: right;
        }
        .note-row-meta { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--color-text-secondary); }
        .note-row-order {
            display: inline-flex; align-items: center; justify-content: center; min-width: 36px; height: 28px; padding: 0 10px;
            border-radius: 999px; background: rgba(219,234,254,0.8); color: #1d4ed8; font-size: 11px; font-weight: 800;
            white-space: nowrap; line-height: 1; flex-shrink: 0;
        }
        .note-card-category {
            display: inline-flex; align-items: center; height: 26px; padding: 0 10px; border-radius: 999px; background: rgba(59,130,246,0.10);
            color: #1d4ed8; font-size: 11px; font-weight: 700; flex-shrink: 0;
            white-space: nowrap; line-height: 1;
        }
        .note-tag {
            display: inline-flex; align-items: center; height: 26px; padding: 0 10px; border-radius: 999px;
            background: rgba(219,234,254,0.8); color: #1e40af; font-size: 11px; font-weight: 700;
            white-space: nowrap; line-height: 1; flex-shrink: 0;
        }
        .note-tag-muted { opacity: 0.72; }
        .note-muted { opacity: 0.45; }
        .note-row-footer { display: none; }
        .note-row-footer-sep { font-size: 10px; color: var(--color-text-secondary); opacity: 0.6; }
        .note-row-footer-meta { font-size: 11px; color: var(--color-text-secondary); }
        .notes-empty {
            padding: 46px 18px; border-radius: 20px; text-align: center; background: rgba(248,250,252,0.82);
            border: 1px dashed rgba(148,163,184,0.26); color: var(--color-text-secondary);
        }
        .notes-pagination { margin-top: 18px; }
        .note-view-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-bottom: 14px; border-bottom: 1px solid rgba(226,232,240,0.8); margin-bottom: 14px; }
        .note-view-content { word-break: break-word; font-size: 14px; line-height: 1.8; color: var(--color-text); max-height: 60vh; overflow-y: auto; }
        .note-view-content p { margin: 0 0 12px; }
        .note-view-content h3, .note-view-content h4, .note-view-content h5 { margin: 18px 0 8px; line-height: 1.35; color: #0f172a; }
        .note-view-content ul { margin: 0 0 12px 20px; padding: 0; }
        .note-view-content li { margin: 4px 0; }
        .note-view-content blockquote { margin: 0 0 12px; padding: 8px 12px; border-left: 3px solid rgba(59,130,246,0.42); background: rgba(239,246,255,0.7); color: #475569; border-radius: 10px; }
        .note-view-content code { padding: 2px 5px; border-radius: 6px; background: rgba(15,23,42,0.06); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }
        .note-view-content pre { overflow-x: auto; padding: 12px; border-radius: 12px; background: #0f172a; color: #e2e8f0; }
        .note-view-content pre code { padding: 0; background: transparent; color: inherit; }
        .note-view-content a { color: #1d4ed8; overflow-wrap: anywhere; }
        .note-reference-panel { margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(226,232,240,0.85); }
        .note-reference-panel h4 { margin: 0 0 10px; font-size: 13px; font-weight: 800; color: var(--color-text); }
        .note-reference-list { display: flex; flex-direction: column; gap: 8px; }
        .note-reference-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 9px 10px; border-radius: 12px; background: rgba(248,250,252,0.92); border: 1px solid rgba(226,232,240,0.9); }
        .note-reference-row span { font-size: 11px; font-weight: 800; color: #1d4ed8; }
        .note-reference-row strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
        .note-reference-row code { color: var(--color-text-secondary); font-size: 11px; }
        .note-view-secondary { font-size: 12px; color: var(--color-text-secondary); margin-left: auto; }
        .note-modal-delete { margin-right: auto; }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .notes-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .notes-layout { grid-template-columns: 1fr; }
            .notes-spotlight { grid-template-columns: 1fr; }
            .notes-spotlight-side { padding-left: 0; border-left: none; padding-top: 4px; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .notes-hero { grid-template-columns: 1fr; padding: 22px 20px; }
            .notes-hero-actions { align-items: flex-start; }
            .notes-summary-grid { grid-template-columns: 1fr; }
            .notes-custom-range { flex-direction: column; align-items: stretch; }
            .notes-date-field { max-width: none; }
            .notes-range-sep { display: none; }
            .notes-filter-bar { grid-template-columns: 1fr; }
            .notes-filter-actions { justify-content: flex-start; }
            .notes-workspace-head { padding: 14px 16px; }
            .notes-workspace-body { padding: 12px; gap: 10px; }
            .notes-spotlight { padding: 14px; gap: 10px; }
            .notes-spotlight-title { font-size: 20px; }
            .notes-spotlight-preview { font-size: 12px; line-height: 1.6; -webkit-line-clamp: 3; }
            .notes-spotlight-tags { gap: 6px; }
            .notes-spotlight-side {
                display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px; padding-left: 0; border-left: none;
            }
            .notes-spotlight-stat { padding: 9px 10px; }
            .note-row { grid-template-columns: 1fr; padding: 12px; gap: 0; }
            .note-row-main { gap: 5px; }
            .note-row-top { flex-direction: row; flex-wrap: wrap; gap: 8px; overflow: hidden; }
            .note-row-title { font-size: 15px; max-width: 100%; }
            .note-row-preview { font-size: 12px; line-height: 1.55; -webkit-line-clamp: 1; }
            .note-row-cat-desktop { display: none; }
            .note-row-tags { display: none; }
            .note-row-side { display: none; }
            .note-row-footer {
                display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin-top: 6px;
            }
            .note-row-order, .note-card-category, .note-tag { height: 22px; padding: 0 7px; font-size: 10px; }
            .notes-list-head { padding: 12px 14px; align-items: flex-start; flex-direction: column; }
            .notes-meter { gap: 6px; }
            .notes-cadence-panel .notes-panel-body { padding-bottom: 14px; }
        `,
        )}
    `,
    );
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
        <section class="notes-hero">
            <div>
                <h2>📝 笔记</h2>
                <p>集中整理灵感、摘录和工作草稿。</p>
                <div class="notes-hero-tags">
                    <span class="notes-hero-tag">${escapeHtml(rangeLabel())} ${summary.total_count || 0} 条笔记</span>
                    <span class="notes-hero-tag">范围尾端近 7 天 ${summary.week_new_count || 0} 条新增</span>
                    <span class="notes-hero-tag">${escapeHtml(_filters.category || '全部分类')}</span>
                </div>
            </div>
            <div class="notes-hero-actions">
                <button class="btn btn-primary" id="notes-add-top" type="button">＋ 新建笔记</button>
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
    const rangeText =
        _activeRange.start && _activeRange.end ? `${_activeRange.start} → ${_activeRange.end}` : '当前范围';
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
    const cadence = _overview?.cadence || [];
    const maxCount = Math.max(1, ...cadence.map((item) => item.count || 0));
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
    const tags = uniqueTextList(note.tags, { caseInsensitive: true });
    const preview = previewText(note.content, 220);
    const spotlightLabel = _page === 1 ? '最新更新' : '本页首条';
    const title = note.title || '(无标题)';
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
    const tags = uniqueTextList(note.tags, { caseInsensitive: true });
    const preview = previewText(note.content, 96);
    const date = formatZonedMonthDay(note.updated_at || note.created_at);
    const wordCount = noteWordCount(note);
    const title = note.title || '(无标题)';
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
        return `<div class="notes-empty">当前筛选下没有笔记。试试清空标签或切换分类。</div>`;
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
                            <p class="notes-list-subtitle">用统一行高查看剩余笔记，优先提升扫读效率。</p>
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
    ensureStyles();

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
    const version = ++_loadVersion;
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

        let list = initialList;
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
    ensureStyles();
    const note = normalizeNote(rawNote);
    if (!note) {
        showToast('无法打开无效笔记', 'warning');
        return null;
    }
    const tags = note.tags;
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
    const closeButton = content.querySelector('#note-close');
    const editButton = content.querySelector('#note-edit');
    const deleteButton = content.querySelector('#note-delete');
    closeButton.onclick = closeModal;
    editButton.onclick = () => {
        closeModal();
        openNoteFormModal(note);
    };
    deleteButton.onclick = async () => {
        if (deleteButton.disabled) return;
        deleteButton.disabled = true;
        closeModal();
        await confirmAndDeleteNote(note, '返回详情', () => openNoteViewModal(note));
    };
    return content;
}

function openNoteFormModal(existing = null) {
    ensureStyles();
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
    const form = content.querySelector('#note-form');
    const saveButton = content.querySelector('#note-modal-save');
    content.querySelector('#note-modal-cancel').onclick = closeModal;

    if (note) {
        const deleteButton = content.querySelector('#note-modal-delete');
        deleteButton.onclick = async () => {
            if (deleteButton.disabled) return;
            deleteButton.disabled = true;
            closeModal();
            await confirmAndDeleteNote(note, '返回编辑', () => openNoteFormModal(note));
        };
    }

    let saving = false;
    const saveNote = async (event) => {
        event?.preventDefault?.();
        if (saving) return;
        const data = getFormData(form) || {};
        const title = textValue(data.title).trim();
        if (!title) {
            showToast('请填写标题', 'warning');
            return;
        }
        const relatedItems = idListFromInput(data.related_items);
        const payload = {
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
                await api.put(`/items/${encodeURIComponent(note.id)}`, payload);
                showToast('笔记已更新', 'success');
            } else {
                await api.post('/items', { type: 'note', ...payload });
                showToast('笔记已创建', 'success');
            }
            closeModal();
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
            const nextRange = RANGE_KEYS.has(requestedRange) ? requestedRange : 'month';
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
        const start = textValue(root.querySelector('#notes-range-start')?.value).trim();
        const end = textValue(root.querySelector('#notes-range-end')?.value).trim();
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
