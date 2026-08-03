"""Pendo Web 迁移页的数据边界、写操作与异步生命周期回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
TRANSFER_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js"
)

TRANSFER_SETUP: Final = r"""
    globalThis.__apiCalls = [];
    globalThis.__apiHandlers = {
        get: async () => ({ data: { logs: [] } }),
        post: async () => ({ data: {} }),
        download: async () => ({ blob: {}, filename: 'backup.pendo.zip' }),
        upload: async () => ({ data: {} }),
    };
    globalThis.__api = {
        get: async (...args) => {
            __apiCalls.push({ method: 'get', args });
            return __apiHandlers.get(...args);
        },
        post: async (...args) => {
            __apiCalls.push({ method: 'post', args });
            return __apiHandlers.post(...args);
        },
    };
    globalThis.__apiDownload = async (...args) => {
        __apiCalls.push({ method: 'download', args });
        return __apiHandlers.download(...args);
    };
    globalThis.__apiUpload = async (...args) => {
        __apiCalls.push({ method: 'upload', args });
        return __apiHandlers.upload(...args);
    };
    globalThis.__toastCalls = [];
    globalThis.__styleCalls = [];
    globalThis.__anchorClicks = 0;
    globalThis.__anchorRemovals = 0;
    globalThis.__appendedAnchors = 0;
    globalThis.__objectUrls = [];
    globalThis.__revokedUrls = [];
    globalThis.URL.createObjectURL = (blob) => {
        const url = `blob:test-${__objectUrls.length + 1}`;
        __objectUrls.push({ blob, url });
        return url;
    };
    globalThis.URL.revokeObjectURL = (url) => __revokedUrls.push(url);
    globalThis.document = {
        body: {
            appendChild() { __appendedAnchors += 1; },
        },
        createElement(tag) {
            if (tag !== 'a') throw new Error('unexpected element');
            return {
                href: '',
                download: '',
                click() { __anchorClicks += 1; },
                remove() { __anchorRemovals += 1; },
            };
        },
    };
    globalThis.__deferred = () => {
        let resolve;
        let reject;
        const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
        return { promise, resolve, reject };
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    };
    globalThis.__makeRoot = ({ nodes = {}, lists = {} } = {}) => ({
        innerHTML: '',
        querySelector(selector) { return nodes[selector] || null; },
        querySelectorAll(selector) { return lists[selector] || []; },
    });
"""


def _transfer_source_for_test() -> str:
    """替换相邻浏览器依赖，并仅为测试暴露迁移页内部契约。"""

    source = TRANSFER_CLIENT.read_text(encoding="utf-8")
    replacements = (
        (
            "import { api, apiDownload, apiUpload } from '../api.js';",
            "const api = globalThis.__api;\n"
            "const apiDownload = globalThis.__apiDownload;\n"
            "const apiUpload = globalThis.__apiUpload;",
        ),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            """import {
    errorMessage,
    isRecord,
    isValidDateInput,
    nonNegativeInteger,
    parseDate,
    trimmedTextValue as textValue,
} from '../utils/format.js';""",
            r"""const isRecord = (value) => value !== null
    && typeof value === 'object'
    && !Array.isArray(value);
const textValue = (value, fallback = '') =>
    typeof value === 'string' ? value.trim() : fallback;
const errorMessage = (error, fallback = '未知错误') => textValue(error?.message, fallback);
const nonNegativeInteger = (value) => {
    try {
        const number = Number(value);
        return Number.isFinite(number) && number >= 0 ? Math.trunc(number) : 0;
    } catch {
        return 0;
    }
};
const parseDate = (value) => {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : new Date(value);
    if (typeof value !== 'string' || !value.trim()) return null;
    const text = value.trim();
    const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T00:00:00` : text);
    if (Number.isNaN(date.getTime())) return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
        const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
        if (key !== text) return null;
    }
    return date;
};
const isValidDateInput = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value ?? '').trim())
    && parseDate(String(value).trim()) !== null;""",
        ),
        (
            "import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } "
            "from '../utils/ui.js';",
            r"""const BREAKPOINTS = { XL: '1200px', MOBILE: '720px', PHONE: '480px' };
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (breakpoint, cssText) => `@media (max-width:${breakpoint}) {${cssText}}`;
const pageShellCss = () => '';""",
        ),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    defaultState as __defaultState,
    downloadExport as __downloadExport,
    executeImportFile as __executeImportFile,
    exportSelection as __exportSelection,
    inspectImportFile as __inspectImportFile,
    loadHistory as __loadHistory,
    loadSamplePage as __loadSamplePage,
    nonNegativeInteger as __nonNegativeInteger,
    normalizeExportPreview as __normalizeExportPreview,
    normalizeImportResult as __normalizeImportResult,
    normalizeInspect as __normalizeInspect,
    normalizeLogs as __normalizeLogs,
    normalizeSamplePage as __normalizeSamplePage,
    previewExport as __previewExport,
    renderExportPreview as __renderExportPreview,
    renderImportExamples as __renderImportExamples,
    renderImportInspect as __renderImportInspect,
    renderImportResult as __renderImportResult,
    renderPage as __renderPage,
    setImportFile as __setImportFile,
    typeLabel as __typeLabel,
};
export function __setTransferTestState({ container, state } = {}) {
    if (arguments[0] && 'container' in arguments[0]) _container = container;
    if (arguments[0] && 'state' in arguments[0]) _state = state;
}
export function __getTransferTestState() {
    return { container: _container, state: _state };
}
"""
    )


def _run_transfer_client(script: str) -> None:
    """在 Node 中执行迁移页真实 ESM 数据、动作和生命周期。"""

    assert_node_esm_contract(
        _transfer_source_for_test(),
        script,
        cwd=ROOT,
        setup=TRANSFER_SETUP,
    )


def test_transfer_page_real_module_imports() -> None:
    """生产模块及其真实依赖图必须可由 ESM 正常解析。"""

    assert_node_esm_contract(
        "export {};",
        "await import('./plugins/pendo/web/static/js/pages/transfer.js');",
        cwd=ROOT,
    )


def test_transfer_normalizes_preview_counts_dates_and_warnings() -> None:
    """预览响应中的未知类型、非法日期和异常计数不得进入页面。"""

    _run_transfer_client(
        r"""
        assert.equal(client.__nonNegativeInteger(Symbol('bad')), 0);
        assert.equal(client.__nonNegativeInteger(Infinity), 0);
        assert.equal(client.__nonNegativeInteger(-2), 0);
        assert.equal(client.__nonNegativeInteger('2.9'), 2);

        const preview = client.__normalizeExportPreview({
            selection: {
                types: ['task', 'task', '<script>'],
                preset: 'broken',
                start: '2026-02-30',
                end: '2026-03-01',
            },
            counts: { task: '3.8', '<script>': 99 },
            total: 999,
            warnings: ['<img src=x>', null, 42],
        });
        assert.deepEqual(preview, {
            selection: { types: ['task'], preset: 'all', start: '', end: '' },
            counts: [{ type: 'task', count: 3 }],
            total: 3,
            warnings: ['<img src=x>'],
        });
        const html = client.__renderExportPreview(preview);
        assert.ok(html.includes('&lt;img src=x&gt;'));
        assert.ok(!html.includes('<img src=x>'));
        assert.ok(!html.includes('<script>'));
        assert.equal(client.__typeLabel('<script>'), '未知类型');
        """
    )


def test_transfer_normalizes_inspect_results_and_logs_before_rendering() -> None:
    """预检、执行结果和历史日志都必须在渲染前限量、校验并转义。"""

    _run_transfer_client(
        r"""
        const errors = Array.from({ length: 102 }, (_, index) => ({
            path: `<file-${index}>`, line: index + 1, message: '<bad>',
        }));
        const inspect = client.__normalizeInspect({
            summary: { types: ['task', 'unknown', 'task'], files: '2' },
            files: [{ path: '<tasks.ndjson>', type: 'task', count: '4', valid: 3 }, null],
            counts: { valid: '3', errors: 102, total_samples: 12 },
            already_imported: 'true',
            warnings: ['<warning>'],
            errors,
            samples: [{ type: 'task', id: '<id>', title: '<title>' }, { type: 'bad' }],
        });
        assert.deepEqual(inspect.summary, { types: ['task'], files: 2 });
        assert.equal(inspect.already_imported, false);
        assert.equal(inspect.errors.length, 100);
        assert.equal(inspect.samples.length, 1);

        const state = client.__defaultState();
        state.import.inspect = inspect;
        state.import.selectedTypes = ['task'];
        const inspectHtml = client.__renderImportInspect(state.import);
        assert.ok(inspectHtml.includes('&lt;tasks.ndjson&gt;'));
        assert.ok(inspectHtml.includes('&lt;warning&gt;'));
        assert.ok(inspectHtml.includes('另有 2 条错误'));
        assert.ok(!inspectHtml.includes('<bad>'));

        const rows = Array.from({ length: 102 }, (_, index) => ({
            type: 'task', id: `id-${index}`, title: `<title-${index}>`, reason: '<reason>',
        }));
        const result = client.__normalizeImportResult({
            summary: { types: ['task', 'bad'] },
            counts: { valid: '102.9', errors: 2 },
            bundle_id: ' bundle-1 ',
            warnings: ['<result-warning>', null],
            errors: [{ path: '<invalid.ndjson>', line: 7, message: '<invalid-row>' }],
            results: { inserted: 102, updated: -1, skipped: Infinity, failed: Symbol('bad') },
            details: { inserted: rows, updated: 'bad' },
        });
        assert.equal(result.details.inserted.length, 100);
        assert.deepEqual(result.counts, { valid: 102, errors: 2 });
        assert.equal(result.bundle_id, 'bundle-1');
        assert.deepEqual(result.results, { inserted: 102, updated: 0, skipped: 0, failed: 0 });
        const resultHtml = client.__renderImportResult(result);
        assert.ok(resultHtml.includes('另有 2 条新增记录'));
        assert.ok(resultHtml.includes('&lt;result-warning&gt;'));
        assert.ok(resultHtml.includes('&lt;invalid.ndjson&gt;'));
        assert.ok(resultHtml.includes('&lt;invalid-row&gt;'));
        assert.ok(resultHtml.includes('&lt;title-0&gt;'));
        assert.ok(!resultHtml.includes('<reason>'));

        const logs = client.__normalizeLogs([
            { action: 'unknown' },
            {
                action: 'import', filename: '<backup>', types: ['task', '<bad>'], record_count: '5.9',
                result_summary: { inserted: 2, updated: 1, skipped: 2 },
                created_at: '2026-05-20T10:00:00',
            },
        ]);
        assert.equal(logs.length, 1);
        assert.equal(logs[0].record_count, 5);
        assert.deepEqual(logs[0].types, ['task']);
        assert.equal(logs[0].created_at instanceof Date, true);
        """
    )


def test_transfer_render_has_accessible_controls_and_static_examples() -> None:
    """页面控件必须具备按钮语义，静态格式示例和响应式样式仍需完整。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        client.__setTransferTestState({ container: root, state });
        client.__renderPage();
        assert.ok(root.innerHTML.includes('type="button" class="transfer-tab active"'));
        assert.ok(root.innerHTML.includes('aria-pressed="true"'));
        assert.ok(root.innerHTML.includes('type="button" class="transfer-btn secondary"'));
        assert.ok(!root.innerHTML.includes('style='));

        const examples = client.__renderImportExamples();
        for (const text of [
            'manifest.json', 'tasks.ndjson', 'event_collections.ndjson', '_schema: 2',
            'amount_cents', 'counter_account_name', 'offset_seconds', '未知字段会在预检阶段报错',
            '稳定自定义字符串', '外部 ID 只作为来源元数据',
        ]) assert.ok(examples.includes(text));

        const css = __styleCalls[0][1];
        assert.ok(css.includes('.transfer-status-banner.warning,'));
        assert.ok(css.includes('.transfer-status-banner.duplicate-warn'));
        assert.ok(css.includes('.transfer-summary-value.range'));
        assert.ok(css.includes('grid-template-columns: 1fr'));
        assert.ok(css.includes('grid-template-columns: repeat(2, minmax(0, 1fr))'));
        """
    )


def test_transfer_export_selection_validates_real_ordered_dates() -> None:
    """自定义导出范围必须是两个真实日期，且开始日期不得晚于结束日期。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        state.export.preset = 'custom';
        const root = __makeRoot();
        client.__setTransferTestState({ container: root, state });

        state.export.start = '2026-02-30';
        state.export.end = '2026-03-01';
        assert.equal(client.__exportSelection().error, '自定义范围需要填写两个有效日期');

        state.export.start = '2026-03-02';
        state.export.end = '2026-03-01';
        assert.equal(client.__exportSelection().error, '开始日期不能晚于结束日期');

        state.export.start = ' 2026-03-01 ';
        state.export.end = '2026-03-02';
        const selection = client.__exportSelection();
        assert.equal(selection.error, '');
        assert.deepEqual(selection.payload.types, ['event', 'task', 'ledger', 'note', 'diary']);
        assert.equal(selection.payload.start, '2026-03-01');
        assert.equal(selection.payload.end, '2026-03-02');
        assert.ok(selection.payload.timezone);
        """
    )


def test_transfer_preview_deduplicates_and_ignores_changed_selection() -> None:
    """预览双击只能请求一次，等待期间选择改变后旧响应不得覆盖当前口径。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        client.__setTransferTestState({ container: root, state });
        const firstDeferred = __deferred();
        __apiHandlers.post = () => firstDeferred.promise;

        const first = client.__previewExport();
        const duplicate = client.__previewExport();
        assert.equal(await duplicate, false);
        assert.equal(__apiCalls.length, 1);
        assert.equal(__apiCalls[0].args[0], '/transfer/export/preview');
        state.export.selectedTypes = ['task'];
        firstDeferred.resolve({
            data: { selection: { types: ['event'], preset: 'month' }, counts: { event: 2 } },
        });
        assert.equal(await first, false);
        assert.equal(state.export.preview, null);
        assert.equal(state.export.loading, false);

        __apiHandlers.post = async () => ({
            data: { selection: { types: ['task'], preset: 'month' }, counts: { task: 3 }, total: 999 },
        });
        assert.equal(await client.__previewExport(), true);
        assert.equal(state.export.preview.total, 3);
        """
    )


def test_transfer_download_revokes_url_and_blocks_duplicate_clicks() -> None:
    """下载期间必须互斥，并在成功后移除临时节点、回收对象 URL。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        client.__setTransferTestState({ container: root, state });
        const deferred = __deferred();
        __apiHandlers.download = () => deferred.promise;

        const first = client.__downloadExport();
        assert.equal(await client.__downloadExport(), false);
        assert.equal(__apiCalls.length, 1);
        deferred.resolve({ blob: { bytes: 1 }, filename: 'safe.pendo.zip' });
        assert.equal(await first, true);
        assert.equal(__anchorClicks, 1);
        assert.equal(__anchorRemovals, 1);
        assert.equal(__appendedAnchors, 1);
        assert.deepEqual(__revokedUrls, ['blob:test-1']);
        assert.equal(state.export.downloading, false);
        """
    )


def test_transfer_reselected_same_file_invalidates_old_inspection() -> None:
    """即使浏览器复用同一个 File 对象，重新选择也必须使旧预检响应失效。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        const file = { name: 'same\r\nfile.pendo.zip' };
        client.__setTransferTestState({ container: root, state });
        client.__setImportFile(file);
        const firstGeneration = state.import.generation;
        const deferred = __deferred();
        __apiHandlers.upload = () => deferred.promise;

        const first = client.__inspectImportFile();
        assert.equal(await client.__inspectImportFile(), false);
        client.__setImportFile(file);
        assert.equal(state.import.generation, firstGeneration + 1);
        deferred.resolve({
            data: {
                summary: { types: ['task'], files: 1 }, files: [],
                counts: { valid: 1, errors: 0, total_samples: 1 }, errors: [], samples: [],
            },
        });
        assert.equal(await first, false);
        assert.equal(state.import.inspect, null);

        __apiHandlers.upload = async () => ({
            data: {
                summary: { types: ['task'], files: 1 }, files: [],
                counts: { valid: 1, errors: 0, total_samples: 1 }, errors: [], samples: [],
            },
        });
        assert.equal(await client.__inspectImportFile(), true);
        assert.deepEqual(state.import.selectedTypes, ['task']);
        const lastCall = __apiCalls.at(-1);
        assert.equal(lastCall.args[0], '/transfer/import/inspect');
        assert.equal(lastCall.args[2]['X-Transfer-Filename'], 'same__file.pendo.zip');
        """
    )


def test_transfer_execute_filters_types_and_normalizes_result() -> None:
    """执行导入只能提交预检允许类型，并规范化响应、锁住重复提交。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        const file = { name: 'bundle.pendo.zip' };
        client.__setTransferTestState({ container: root, state });
        client.__setImportFile(file);
        state.import.inspect = client.__normalizeInspect({
            summary: { types: ['task'], files: 1 }, files: [],
            counts: { valid: 1, errors: 0, total_samples: 1 }, errors: [], samples: [],
        });
        state.import.selectedTypes = ['task', 'event', '<bad>'];
        state.history.loaded = true;
        const deferred = __deferred();
        __apiHandlers.upload = () => deferred.promise;

        const first = client.__executeImportFile();
        assert.equal(await client.__executeImportFile(), false);
        assert.equal(__apiCalls.length, 1);
        const options = JSON.parse(__apiCalls[0].args[2]['X-Transfer-Options']);
        assert.deepEqual(options, {
            types: ['task'], conflict_policy: 'isolate', invalid_policy: 'abort', force: false,
        });
        deferred.resolve({
            data: {
                summary: { types: ['task', '<bad>'] },
                results: { inserted: '1', updated: -1, skipped: 0, failed: 0 },
                details: { inserted: [{ type: 'task', id: '<id>', title: '<title>' }] },
            },
        });
        assert.equal(await first, true);
        assert.equal(state.import.result.results.inserted, 1);
        assert.deepEqual(state.import.result.summary.types, ['task']);
        assert.equal(state.history.loaded, false);
        assert.equal(state.import.executing, false);
        """
    )


def test_transfer_sample_pagination_covers_records_without_gap() -> None:
    """预检的 5 条首屏样例与后续分页必须使用同一页大小，不能漏掉第 6-20 条。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        const root = __makeRoot();
        const file = { name: 'bundle.pendo.zip' };
        client.__setTransferTestState({ container: root, state });
        client.__setImportFile(file);
        state.import.inspect = client.__normalizeInspect({
            summary: { types: ['task'], files: 1 }, files: [],
            counts: { valid: 12, errors: 0, total_samples: 12 }, errors: [],
            samples: Array.from({ length: 5 }, (_, index) => ({ type: 'task', id: `${index + 1}` })),
        });
        assert.equal(state.import.samplePageSize, 5);
        __apiHandlers.upload = async (_path, _file, headers) => ({
            data: {
                samples: [{ type: 'task', id: headers['X-Transfer-Page'] === '2' ? '6' : '11' }],
                page: Number(headers['X-Transfer-Page']), page_size: 5, total: 12,
            },
        });

        assert.equal(await client.__loadSamplePage(2), true);
        assert.equal(__apiCalls[0].args[2]['X-Transfer-Page'], '2');
        assert.equal(__apiCalls[0].args[2]['X-Transfer-Page-Size'], '5');
        assert.equal(state.import.paginatedSamples.samples[0].id, '6');

        assert.equal(await client.__loadSamplePage(99), true);
        assert.equal(__apiCalls[1].args[2]['X-Transfer-Page'], '3');
        assert.equal(state.import.paginatedSamples.samples[0].id, '11');
        """
    )


def test_transfer_history_deduplicates_and_destroy_ignores_late_response() -> None:
    """历史请求期间应去重，页面销毁后迟到响应不得恢复状态或提示。"""

    _run_transfer_client(
        r"""
        const state = client.__defaultState();
        state.tab = 'history';
        const root = __makeRoot();
        client.__setTransferTestState({ container: root, state });
        const deferred = __deferred();
        __apiHandlers.get = () => deferred.promise;

        const first = client.__loadHistory();
        assert.equal(await client.__loadHistory(), false);
        assert.equal(__apiCalls.length, 1);
        client.destroy();
        deferred.resolve({ data: { logs: [{ action: 'import', filename: '<late>' }] } });
        assert.equal(await first, false);
        assert.equal(client.__getTransferTestState().state, null);
        assert.equal(__toastCalls.length, 0);
        """
    )
