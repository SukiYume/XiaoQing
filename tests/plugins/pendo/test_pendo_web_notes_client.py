"""Pendo Web 笔记页的数据边界、表单操作与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_timezone_test_support import inline_timezone_runtime

ROOT: Final = REPOSITORY_ROOT
NOTES_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "notes.js"
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)

NOTES_SETUP: Final = r"""
    globalThis.__api = {
        get: async () => ({ data: {} }),
        post: async () => ({ data: {} }),
        put: async () => ({ data: {} }),
        delete: async () => ({ data: {} }),
    };
    globalThis.__toastCalls = [];
    globalThis.__modalCalls = [];
    globalThis.__modalContent = null;
    globalThis.__closeModalCount = 0;
    globalThis.__confirmResult = true;
    globalThis.__formData = {};
    globalThis.__builtFields = [];
    globalThis.__styleCalls = [];
    globalThis.__paginationCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__customSelectHandlers = null;
    globalThis.__dispatchedEvents = [];
    globalThis.__rangeBounds = { start: '2020-01-01', end: '2026-03-15' };
    globalThis.__rangeBoundsCalls = [];
    globalThis.__subscribeDataChanges = (_type, callback) => {
        __dataChangeCallback = callback;
        return () => { __unsubscribeCount += 1; };
    };
    globalThis.window = {
        dispatchEvent(event) { __dispatchedEvents.push(event); return true; },
    };
    globalThis.CustomEvent = class {
        constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    };
    globalThis.__deferred = () => {
        let resolve;
        let reject;
        const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
        return { promise, resolve, reject };
    };
    globalThis.__makeControl = (extra = {}) => ({
        disabled: false,
        value: '',
        dataset: {},
        onclick: null,
        onsubmit: null,
        listeners: {},
        addEventListener(type, listener) {
            (this.listeners[type] ||= []).push(listener);
        },
        ...extra,
    });
    globalThis.__makeRoot = ({ nodes = {}, lists = {} } = {}) => ({
        innerHTML: '',
        querySelector(selector) { return nodes[selector] || null; },
        querySelectorAll(selector) { return lists[selector] || []; },
    });
"""


def _notes_source_for_test() -> str:
    """替换浏览器相邻依赖，并嵌入真实共享日期格式实现。"""

    source = NOTES_CLIENT.read_text(encoding="utf-8")
    timezone_runtime = inline_timezone_runtime(TIMEZONE_CLIENT)
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{
    errorMessage,
    finiteNumber,
    noteCadenceSubtitle,
    nonNegativeInteger,
    previewText,
    textValue,
}} = (() => {{
{format_source}
    return {{
        errorMessage,
        finiteNumber,
        noteCadenceSubtitle,
        nonNegativeInteger,
        previewText,
        textValue,
    }};
}})();
"""
    replacements = (
        ("import { api } from '../api.js';", "const api = globalThis.__api;"),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            "import { showModal, closeModal, showConfirmModal, safeHtml } "
            "from '../components/modal.js';",
            """const showModal = (...args) => {
    globalThis.__modalCalls.push(args);
    return globalThis.__modalContent;
};
const closeModal = () => { globalThis.__closeModalCount += 1; };
const showConfirmModal = async () => globalThis.__confirmResult;
const safeHtml = (value) => value;""",
        ),
        (
            "import { buildFormHTML, getFormData, initFormInteractions } "
            "from '../components/form.js';",
            """const buildFormHTML = (fields) => {
    globalThis.__builtFields = fields;
    return '<div class="stub-form-fields"></div>';
};
const getFormData = () => ({ ...globalThis.__formData });
const initFormInteractions = () => {};""",
        ),
        (
            "import { renderPagination } from '../components/pagination.js';",
            "const renderPagination = (...args) => globalThis.__paginationCalls.push(args);",
        ),
        (
            "import { renderCustomSelect, initCustomSelects } "
            "from '../components/custom_select.js';",
            """const renderCustomSelect = ({ id, options = [], selected = '' }) => `
    <div class="stub-select" id="${escapeHtml(id)}" data-value="${escapeHtml(selected)}">
        ${options.map((option) => `<span data-option="${escapeHtml(option.value)}">${escapeHtml(option.label)}</span>`).join('')}
    </div>`;
const initCustomSelects = (_root, handlers) => {
    globalThis.__customSelectHandlers = handlers;
};""",
        ),
        (
            """import {
    errorMessage,
    finiteNumber,
    noteCadenceSubtitle,
    nonNegativeInteger,
    previewText,
    textValue,
} from '../utils/format.js';""",
            format_runtime,
        ),
        (
            "import { formatZonedDateTime, formatZonedMonthDay } from '../utils/timezone.js';",
            timezone_runtime,
        ),
        (
            "import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, "
            "todayRangeKey } from '../utils/date_ranges.js';",
            """const RANGE_PRESET_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'quarter', label: '本季' },
    { key: 'year', label: '今年' },
    { key: 'last_year', label: '去年' },
    { key: 'custom', label: '自定义' },
    { key: 'all', label: '全部' },
];
const todayRangeKey = () => '2026-03-15';
const normalizeDateKey = (value) => {
    if (typeof value !== 'string' || !/^\\d{4}-\\d{2}-\\d{2}$/.test(value.trim())) return '';
    const key = value.trim();
    const date = new Date(`${key}T00:00:00Z`);
    return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === key ? key : '';
};
const derivePresetRange = (preset, options = {}) => {
    const today = normalizeDateKey(options.today) || todayRangeKey();
    if (preset === 'custom') {
        const start = normalizeDateKey(options.customStart);
        const end = normalizeDateKey(options.customEnd);
        if (start && end) return { start, end };
        if (!options.customFallback) return { start: '', end: '' };
        return derivePresetRange(options.customFallback, { today, customFallback: '' });
    }
    if (preset === 'all') return { start: '1970-01-01', end: today };
    if (preset === 'week') return { start: '2026-03-09', end: '2026-03-15' };
    if (preset === 'month') return { start: '2026-03-01', end: '2026-03-31' };
    if (preset === 'quarter') return { start: '2026-01-01', end: '2026-03-31' };
    if (preset === 'last_year') return { start: '2025-01-01', end: '2025-12-31' };
    if (preset === 'year') return { start: '2026-01-01', end: '2026-12-31' };
    return { start: today, end: today };
};
const fetchItemRangeBounds = async (_apiClient, options) => {
    globalThis.__rangeBoundsCalls.push(options);
    return { ...globalThis.__rangeBounds };
};""",
        ),
        (
            "import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, "
            "subscribeDataChanges } from '../utils/ui.js';",
            """const BREAKPOINTS = { XL: '1200px', MOBILE: '720px', PHONE: '560px' };
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (breakpoint, cssText) => `@media (max-width:${breakpoint}) {${cssText}}`;
const pageShellCss = () => '';
const subscribeDataChanges = (...args) => globalThis.__subscribeDataChanges(...args);""",
        ),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    attachListeners as __attachListeners,
    confirmAndDeleteNote as __confirmAndDeleteNote,
    deriveRangeDates,
    ensureStyles as __ensureStyles,
    fetchNotes,
    fetchOverview,
    loadAndRender as __loadAndRender,
    normalizeNote,
    normalizeOverview,
    openNoteFormModal as __openNoteFormModal,
    renderCadencePanel,
    renderCategoryPanel,
    renderMarkdown,
    renderNoteReferences,
    renderNoteRow,
    renderSpotlight,
};
export function __setNotesTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('items' in state) _items = state.items;
    if ('overview' in state) _overview = state.overview;
    if ('total' in state) _total = state.total;
    if ('page' in state) _page = state.page;
    if ('loading' in state) _loading = state.loading;
    if ('filters' in state) _filters = { ..._filters, ...state.filters };
    if ('activeRange' in state) _activeRange = state.activeRange;
    _pendingDeletes.clear();
}
export function __getNotesTestState() {
    return {
        container: _container,
        items: _items,
        overview: _overview,
        total: _total,
        page: _page,
        loading: _loading,
        filters: { ..._filters },
        activeRange: { ..._activeRange },
    };
}
"""
    )


def _run_notes_client(script: str) -> None:
    """在 Node 中执行笔记页真实 ESM 数据、渲染、表单和生命周期。"""

    assert_node_esm_contract(
        _notes_source_for_test(),
        script,
        cwd   = ROOT,
        setup = NOTES_SETUP,
    )


def test_notes_normalization_markdown_and_reference_boundaries() -> None:
    """损坏响应不得污染状态，Markdown 只允许安全子集。"""

    _run_notes_client(
        r"""
        const note = client.normalizeNote({
            id: ' note/a ',
            title: '<img src=x onerror=alert(1)>',
            category: '<script>分类</script>',
            tags: ['工作', '工作', 'WORK', null],
            content: '<script>alert(1)</script> **加粗** [安全](https://example.com/a?x=1&y=2) [危险](javascript:alert(1))',
            references: [
                { id: 'event/1', type: 'event', title: '<b>日程</b>' },
                null,
                { id: '' },
            ],
            related_items: ['event/1', 'task/2', 'task/2'],
        });
        assert.equal(note.id, 'note/a');
        assert.deepEqual(note.tags, ['工作', 'WORK']);
        assert.deepEqual(note.related_items, ['event/1', 'task/2']);
        assert.equal(client.normalizeNote({ id: Symbol('bad') }), null);
        assert.equal(client.normalizeNote(null), null);

        const overview = client.normalizeOverview({
            summary: {
                total_count: Number.POSITIVE_INFINITY,
                week_new_count: -4,
                average_length: '12.4',
                tagged_rate: 8,
            },
            categories: [{ category: '<img>', count: '3.9', share: -2 }, null],
            hot_tags: [{ tag: '<script>tag</script>', count: Number.NaN }],
            cadence: [{ label: '<svg>', count: '5.8' }, { label: '', count: 9 }],
            cadence_granularity: 'hostile',
            all_categories: ['工作', '工作', 3],
        });
        assert.deepEqual(overview.summary, {
            total_count: 0,
            week_new_count: 0,
            average_length: 12.4,
            tagged_rate: 1,
        });
        assert.equal(overview.categories[0].count, 3);
        assert.equal(overview.categories[0].share, 0);
        assert.equal(overview.cadence[0].count, 5);
        assert.equal(overview.cadence_granularity, 'day');

        const markdown = client.renderMarkdown(note.content);
        assert.ok(markdown.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
        assert.ok(markdown.includes('<strong>加粗</strong>'));
        assert.ok(markdown.includes('href="https://example.com/a?x=1&amp;y=2"'));
        assert.ok(!markdown.includes('href="javascript:'));
        assert.ok(!markdown.includes('<script>'));

        const references = client.renderNoteReferences(note);
        assert.ok(references.includes('&lt;b&gt;日程&lt;/b&gt;'));
        assert.ok(references.includes('task/2'));
        assert.ok(!references.includes('<b>日程</b>'));
        """
    )


def test_notes_fetches_share_filters_and_render_safe_native_controls() -> None:
    """列表与概览共享筛选，模板只渲染归一化文本和原生按钮。"""

    _run_notes_client(
        r"""
        client.__setNotesTestState({
            filters: { category: '知识', tag: '阅读', keyword: '关键字' },
            page: 2,
        });
        const calls = [];
        __api.get = async (path, params) => {
            calls.push({ path, params });
            if (path === '/stats/notes/overview') {
                return {
                    data: {
                        summary: { total_count: '2', tagged_rate: 0.5 },
                        categories: [{ category: '<分类>', count: 2, share: 0.75 }],
                        hot_tags: [{ tag: '<标签>', count: 2 }],
                        cadence: [{ label: '<今天>', count: 2 }],
                        all_categories: ['知识'],
                    },
                };
            }
            return {
                data: {
                    total: '4',
                    items: [null, {
                        id: 'note/1',
                        title: '<script>标题</script>',
                        category: '<分类>',
                        tags: ['<标签>'],
                        content: '<img src=x>',
                        updated_at: '2026-03-15T08:00:00',
                    }],
                },
            };
        };
        const range = { start: '2026-03-01', end: '2026-03-15' };
        const [overview, list] = await Promise.all([
            client.fetchOverview(range),
            client.fetchNotes(2, range),
        ]);
        assert.deepEqual(calls[0].params, {
            today: '2026-03-15',
            start_date: '2026-03-01',
            end_date: '2026-03-15',
            category: '知识',
            tags: '阅读',
        });
        assert.equal(calls[1].params.keyword, '关键字');
        assert.equal(calls[1].params.date_field, 'created_at');
        assert.equal(calls[1].params.start_date, '2026-03-01T00:00:00');
        assert.equal(calls[1].params.end_date, '2026-03-15T23:59:59');
        assert.equal(list.items.length, 1);
        assert.equal(list.total, 4);

        client.__setNotesTestState({ overview, items: list.items, total: list.total });
        const spotlight = client.renderSpotlight(list.items[0]);
        const row = client.renderNoteRow(list.items[0], 1);
        const cadence = client.renderCadencePanel();
        const categories = client.renderCategoryPanel();
        for (const html of [spotlight, row, cadence, categories]) {
            assert.ok(!html.includes('<script>'));
            assert.ok(!html.includes('style="'));
        }
        assert.ok(spotlight.includes('<button class="note-card notes-spotlight"'));
        assert.ok(spotlight.includes('type="button"'));
        assert.ok(spotlight.includes('data-open-note="note/1"'));
        assert.ok(!spotlight.includes('data-id='));
        assert.ok(spotlight.includes('&lt;script&gt;标题&lt;/script&gt;'));
        assert.ok(cadence.includes('&lt;今天&gt;'));
        assert.ok(categories.includes('&lt;分类&gt;'));
        assert.ok(categories.includes('notes-level-8'));
        """
    )


def test_notes_latest_load_wins_and_destroy_blocks_late_results() -> None:
    """快速切换筛选及销毁页面后，旧请求不能覆盖当前 DOM。"""

    _run_notes_client(
        r"""
        const root = __makeRoot();
        client.__setNotesTestState({
            container: root,
            overview: client.normalizeOverview({}),
            filters: { category: '旧分类' },
        });
        const pending = [];
        __api.get = (path, params) => {
            const deferred = __deferred();
            pending.push({ path, params, ...deferred });
            return deferred.promise;
        };

        const first = client.__loadAndRender();
        await __flushPromises();
        assert.equal(pending.length, 2);
        client.__setNotesTestState({ filters: { category: '新分类' } });
        const second = client.__loadAndRender();
        await __flushPromises();
        assert.equal(pending.length, 4);

        pending[2].resolve({ data: { summary: { total_count: 1 }, all_categories: ['新分类'] } });
        pending[3].resolve({ data: { total: 1, items: [{ id: 'new', title: '新结果' }] } });
        await second;
        const latestHtml = root.innerHTML;
        assert.ok(latestHtml.includes('新结果'));

        pending[0].resolve({ data: { summary: { total_count: 1 }, all_categories: ['旧分类'] } });
        pending[1].resolve({ data: { total: 1, items: [{ id: 'old', title: '旧结果' }] } });
        await first;
        assert.equal(root.innerHTML, latestHtml);
        assert.ok(!root.innerHTML.includes('旧结果'));

        const late = [];
        __api.get = (path) => {
            const deferred = __deferred();
            late.push({ path, ...deferred });
            return deferred.promise;
        };
        const third = client.__loadAndRender();
        await __flushPromises();
        const htmlBeforeDestroy = root.innerHTML;
        client.destroy();
        late[0].resolve({ data: { summary: { total_count: 1 } } });
        late[1].resolve({ data: { total: 1, items: [{ id: 'late', title: '迟到结果' }] } });
        await third;
        assert.equal(root.innerHTML, htmlBeforeDestroy);
        assert.ok(!root.innerHTML.includes('迟到结果'));
        """
    )


def test_notes_page_clamps_deleted_last_page_and_subscription_is_idempotent() -> None:
    """删除末页数据后应回到有效页，重复挂载和销毁只保留一个订阅。"""

    _run_notes_client(
        r"""
        const pageCalls = [];
        __api.get = async (path, params) => {
            if (path === '/stats/notes/overview') return { data: { summary: {} } };
            pageCalls.push(params.page);
            return params.page === 3
                ? { data: { total: 1, items: [] } }
                : { data: { total: 1, items: [{ id: 'only', title: '唯一笔记' }] } };
        };
        const root = __makeRoot();
        client.__setNotesTestState({
            container: root,
            overview: client.normalizeOverview({}),
            page: 3,
        });
        await client.__loadAndRender();
        assert.deepEqual(pageCalls, [3, 1]);
        assert.equal(client.__getNotesTestState().page, 1);
        assert.ok(root.innerHTML.includes('唯一笔记'));

        pageCalls.length = 0;
        const firstRoot = __makeRoot();
        await client.render(firstRoot);
        assert.equal(typeof __dataChangeCallback, 'function');
        const secondRoot = __makeRoot();
        await client.render(secondRoot);
        assert.equal(__unsubscribeCount, 1);
        client.destroy();
        assert.equal(__unsubscribeCount, 2);
        await assert.rejects(() => client.render(null), /有效的容器元素/);

        client.__ensureStyles();
        const css = __styleCalls.at(-1)[1];
        assert.ok(css.includes('font-size: clamp(24px, 1.9vw, 30px)'));
        assert.ok(css.includes('.note-row-footer'));
        assert.ok(css.includes('@media (max-width:720px)'));
        """
    )


def test_notes_custom_range_rejects_invalid_dates_and_ime_enter() -> None:
    """自定义日期必须完整有效且有序，输入法组合中的 Enter 不提交。"""

    _run_notes_client(
        r"""
        const start = __makeControl({ value: '2026-02-30' });
        const end = __makeControl({ value: '2026-03-10' });
        const apply = __makeControl();
        const root = __makeRoot({
            nodes: {
                '#notes-range-start': start,
                '#notes-range-end': end,
                '#notes-range-apply': apply,
            },
        });
        client.__setNotesTestState({
            container: root,
            overview: client.normalizeOverview({}),
            filters: {
                range: 'custom',
                customStart: '2026-03-01',
                customEnd: '2026-03-15',
            },
        });
        let getCalls = 0;
        __api.get = async (path) => {
            getCalls += 1;
            return path === '/items' ? { data: { items: [], total: 0 } } : { data: {} };
        };
        client.__attachListeners();

        await apply.onclick();
        assert.equal(getCalls, 0);
        assert.ok(__toastCalls.at(-1)[0].includes('有效日期'));

        start.value = '2026-03-20';
        end.value = '2026-03-10';
        await apply.onclick();
        assert.equal(getCalls, 0);
        assert.ok(__toastCalls.at(-1)[0].includes('不能晚于'));

        const composingEvent = {
            key: 'Enter', isComposing: true, preventDefault() { throw new Error('不应提交'); },
        };
        await start.listeners.keydown[0](composingEvent);
        assert.equal(getCalls, 0);

        start.value = '2026-03-01';
        end.value = '2026-03-10';
        await apply.onclick();
        assert.equal(getCalls, 2);
        assert.deepEqual(client.__getNotesTestState().activeRange, {
            start: '2026-03-01', end: '2026-03-10',
        });
        """
    )


def test_note_view_escapes_content_and_deletes_once_with_encoded_id() -> None:
    """详情正文和关联项必须安全，双击删除只能发出一次编码请求和一次广播。"""

    _run_notes_client(
        r"""
        const buttons = new Map([
            ['#note-close', __makeControl()],
            ['#note-edit', __makeControl()],
            ['#note-delete', __makeControl()],
        ]);
        __modalContent = { querySelector: (selector) => buttons.get(selector) || null };
        const confirmation = __deferred();
        __confirmResult = confirmation.promise;
        const deleteCalls = [];
        __api.delete = async (...args) => { deleteCalls.push(args); return { data: {} }; };

        client.openNoteViewModal({
            id: 'note/a b?',
            title: '<script>标题</script>',
            content: '<img src=x onerror=alert(1)>',
            references: [{ id: 'task/1', type: 'task', title: '<b>关联</b>' }],
        });
        const [, body, options] = __modalCalls.at(-1);
        assert.ok(body.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(body.includes('&lt;b&gt;关联&lt;/b&gt;'));
        assert.ok(!body.includes('<script>'));
        assert.ok(!body.includes('style="'));
        assert.ok(options.footer.includes('type="button"'));

        const first = buttons.get('#note-delete').onclick();
        const second = buttons.get('#note-delete').onclick();
        confirmation.resolve(true);
        await Promise.all([first, second]);
        assert.deepEqual(deleteCalls, [['/items/note%2Fa%20b%3F']]);
        assert.equal(__dispatchedEvents.length, 1);
        assert.equal(__dispatchedEvents[0].detail.type, 'note');
        assert.equal(__closeModalCount, 1);

        client.openNoteViewModal({ title: '缺少 ID' });
        assert.ok(__toastCalls.at(-1)[0].includes('无效笔记'));
        """
    )


def test_note_form_validates_payload_and_prevents_duplicate_create_or_edit() -> None:
    """新增和编辑应收敛字段、编码路径，并阻止双击重复写入。"""

    _run_notes_client(
        r"""
        const makeFormModal = (editing = false) => {
            const nodes = new Map([
                ['#note-form', __makeControl()],
                ['#note-modal-cancel', __makeControl()],
                ['#note-modal-save', __makeControl()],
            ]);
            if (editing) nodes.set('#note-modal-delete', __makeControl());
            __modalContent = { querySelector: (selector) => nodes.get(selector) || null };
            return nodes;
        };

        let nodes = makeFormModal();
        __formData = { title: '   ' };
        client.__openNoteFormModal();
        await nodes.get('#note-modal-save').onclick();
        assert.ok(__toastCalls.at(-1)[0].includes('请填写标题'));

        const create = __deferred();
        const postCalls = [];
        __api.post = (...args) => {
            postCalls.push(args);
            return create.promise;
        };
        __formData = {
            title: '  新笔记  ',
            category: '  学习  ',
            tags: '阅读, 阅读，READING',
            related_items: 'task/1, task/1，event/2',
            content: '正文',
            ignored: '不得透传',
        };
        const firstCreate = nodes.get('#note-modal-save').onclick();
        const secondCreate = nodes.get('#note-modal-save').onclick();
        assert.equal(postCalls.length, 1);
        create.resolve({ data: {} });
        await Promise.all([firstCreate, secondCreate]);
        const [createPath, createPayload] = postCalls[0];
        assert.equal(createPath, '/items');
        assert.deepEqual(createPayload, {
            type: 'note',
            title: '新笔记',
            category: '学习',
            tags: ['阅读', 'READING'],
            related_items: ['task/1', 'event/2'],
            references: [
                { kind: 'item', id: 'task/1' },
                { kind: 'item', id: 'event/2' },
            ],
            content: '正文',
        });
        assert.ok(!('ignored' in createPayload));
        assert.equal(__dispatchedEvents.length, 1);

        nodes = makeFormModal(true);
        __formData = {
            title: '更新后', category: '', tags: '', related_items: '', content: '新正文',
        };
        const putCalls = [];
        __api.put = async (...args) => { putCalls.push(args); return { data: {} }; };
        client.__openNoteFormModal({ id: 'note/a b?', title: '旧标题' });
        await nodes.get('#note-modal-save').onclick();
        assert.equal(putCalls[0][0], '/items/note%2Fa%20b%3F');
        assert.equal(putCalls[0][1].title, '更新后');
        assert.equal(__dispatchedEvents.length, 2);
        """
    )
