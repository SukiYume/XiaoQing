"""Pendo Web 待办页的数据边界、写操作与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_timezone_test_support import inline_timezone_runtime

ROOT: Final = REPOSITORY_ROOT
TASKS_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js"
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)

TASKS_SETUP: Final = r"""
    globalThis.__now = new Date('2026-05-20T12:00:00');
    globalThis.__apiCalls = [];
    globalThis.__apiHandlers = {
        get: async () => ({ data: { all_tasks: [] } }),
        put: async () => ({ data: {} }),
        post: async () => ({ data: {} }),
        delete: async () => ({ data: {} }),
    };
    globalThis.__api = Object.fromEntries(
        ['get', 'put', 'post', 'delete'].map((method) => [method, async (...args) => {
            __apiCalls.push({ method, args });
            return __apiHandlers[method](...args);
        }]),
    );
    globalThis.__toastCalls = [];
    globalThis.__styleCalls = [];
    globalThis.__events = [];
    globalThis.__subscriptions = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__modalCalls = [];
    globalThis.__closeModalCount = 0;
    globalThis.__modalContent = null;
    globalThis.__formData = {};
    globalThis.__confirmResult = true;

    globalThis.CustomEvent = class CustomEvent {
        constructor(type, options = {}) {
            this.type = type;
            this.detail = options.detail;
        }
    };
    globalThis.window = {
        dispatchEvent(event) {
            __events.push(event);
            return true;
        },
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
    globalThis.__makeButton = (extra = {}) => ({
        disabled: false,
        isConnected: true,
        dataset: {},
        attributes: {},
        _listeners: {},
        addEventListener(type, listener) { this._listeners[type] = listener; },
        click() { return this._listeners.click?.({ target: this, stopPropagation() {} }); },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        removeAttribute(name) { delete this.attributes[name]; },
        getAttribute(name) { return this.attributes[name] ?? null; },
        ...extra,
    });
    globalThis.__makeRoot = ({ nodes = {}, lists = {} } = {}) => ({
        innerHTML: '',
        onclick: null,
        querySelector(selector) { return nodes[selector] || null; },
        querySelectorAll(selector) { return lists[selector] || []; },
    });
"""


def _tasks_source_for_test() -> str:
    """替换相邻浏览器依赖，并仅为测试暴露待办页内部契约。"""

    source = TASKS_CLIENT.read_text(encoding="utf-8")
    timezone_runtime = inline_timezone_runtime(TIMEZONE_CLIENT)
    replacements = (
        ("import { api } from '../api.js';", "const api = globalThis.__api;"),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            "import { showModal, closeModal, showConfirmModal, safeHtml } "
            "from '../components/modal.js';",
            r"""const showModal = (...args) => {
    globalThis.__modalCalls.push(args);
    return globalThis.__modalContent;
};
const closeModal = () => { globalThis.__closeModalCount += 1; };
const showConfirmModal = async (...args) => {
    globalThis.__modalCalls.push(args);
    return globalThis.__confirmResult;
};
const safeHtml = (value) => value;""",
        ),
        (
            "import { buildFormHTML, getFormData, initFormInteractions } "
            "from '../components/form.js';",
            r"""const buildFormHTML = (fields) => JSON.stringify(fields);
const getFormData = () => globalThis.__formData;
const initFormInteractions = () => {};""",
        ),
        (
            "import { renderCustomSelect, initCustomSelects } "
            "from '../components/custom_select.js';",
            r"""const renderCustomSelect = ({ id }) => `<div data-select="${id}"></div>`;
const initCustomSelects = () => {};""",
        ),
        (
            """import {
    errorMessage,
    isRecord,
    pad2,
    parseDate,
    trimmedTextValue as textValue,
} from '../utils/format.js';""",
            r"""const isRecord = (value) => value !== null
    && typeof value === 'object'
    && !Array.isArray(value);
const textValue = (value, fallback = '') =>
    typeof value === 'string' ? value.trim() : fallback;
const errorMessage = (error, fallback = '未知错误') => textValue(error?.message, fallback);
const pad2 = (value) => String(value).padStart(2, '0');
const parseDate = (value) => {
    if (!value) return null;
    const text = typeof value === 'string' ? value.trim() : value;
    const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T00:00:00` : text);
    return Number.isNaN(date.getTime()) ? null : date;
};""",
        ),
        (
            "import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, "
            "subscribeDataChanges } from '../utils/ui.js';",
            r"""const BREAKPOINTS = { XL: '1200px', MOBILE: '720px' };
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (breakpoint, cssText) => `@media (max-width:${breakpoint}) {${cssText}}`;
const pageShellCss = () => '';
const subscribeDataChanges = (type, refresh) => {
    globalThis.__subscriptions.push({ type, refresh });
    return () => { globalThis.__unsubscribeCount += 1; };
};""",
        ),
        (
            "import { fetchUserTimeZone, zonedDateTimeToInput, zonedInputToUtcIso } "
            "from '../utils/timezone.js';",
            timezone_runtime,
        ),
        ("const TODAY = () => new Date();", "const TODAY = () => new Date(globalThis.__now);"),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    attachListeners as __attachListeners,
    boardCardHTML as __boardCardHTML,
    deriveDisplayModel as __deriveDisplayModel,
    loadAndRender as __loadAndRender,
    normalizeOverview as __normalizeOverview,
    normalizeTaskPayload as __normalizeTaskPayload,
    parseIsoDate as __parseIsoDate,
    planDateMatches as __planDateMatches,
    renderPage as __renderPage,
    taskRowHTML as __taskRowHTML,
    updateTaskStatus as __updateTaskStatus,
};
export function __setTasksTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('overview' in state) _overview = state.overview;
    if ('loading' in state) _loading = state.loading;
    if ('loadError' in state) _loadError = state.loadError;
    if ('viewMode' in state) _viewMode = state.viewMode;
    if ('filters' in state) _filters = { ...DEFAULT_FILTERS, ...state.filters };
    if ('loadVersion' in state) _loadVersion = state.loadVersion;
    if (state.clearPending) _pendingTaskIds.clear();
}
export function __getTasksTestState() {
    return {
        container: _container,
        overview: _overview,
        loading: _loading,
        loadError: _loadError,
        viewMode: _viewMode,
        filters: { ..._filters },
        loadVersion: _loadVersion,
        pendingTaskIds: [..._pendingTaskIds],
    };
}
"""
    )


def _run_tasks_client(script: str) -> None:
    """在 Node 中执行待办页真实 ESM 数据、动作和生命周期。"""

    assert_node_esm_contract(
        _tasks_source_for_test(),
        script,
        cwd=ROOT,
        setup=TASKS_SETUP,
    )


def test_tasks_page_real_module_imports() -> None:
    """生产模块及其真实依赖图必须可由 ESM 正常解析。"""

    assert_node_esm_contract(
        "export {};",
        "await import('./plugins/pendo/web/static/js/pages/tasks.js');",
        cwd=ROOT,
    )


def test_tasks_normalizes_calendar_dates_and_modal_payload() -> None:
    """非法日历日期和表单额外字段不得进入接口载荷。"""

    _run_tasks_client(
        r"""
        assert.equal(client.__parseIsoDate('2026-02-29'), null);
        assert.equal(client.__parseIsoDate('2024-02-29') instanceof Date, true);
        assert.equal(client.__parseIsoDate('2026-2-09'), null);

        assert.throws(() => client.__normalizeTaskPayload({
            title: ' 任务 ', content: ' 备注 ', category: ' ', status: 'broken',
            priority: '99', plan_date: '2026-02-30', deadline_at: 'bad', extra: 'drop-me',
        }, 'Asia/Shanghai'), /有效的截止时间/);
        assert.deepEqual(client.__normalizeTaskPayload({
            title: ' 任务 ', content: ' 备注 ', category: ' ', status: 'broken',
            priority: '99', plan_date: '2026-02-30', deadline_at: '', extra: 'drop-me',
        }, 'Asia/Shanghai'), {
            title: '任务', content: '备注', category: '未分类', status: 'open',
            priority: 3, plan_date: null, deadline_at: null,
        });
        assert.deepEqual(client.__normalizeTaskPayload({
            title: '有效', status: 'cancelled', priority: '2',
            plan_date: '2024-02-29', deadline_at: '2024-02-29T18:30',
        }, 'Asia/Shanghai'), {
            title: '有效', content: '', category: '未分类', status: 'cancelled',
            priority: 2, plan_date: '2024-02-29', deadline_at: '2024-02-29T10:30:00+00:00',
        });
        """
    )


def test_tasks_normalizes_and_deduplicates_overview_records() -> None:
    """损坏记录、空 ID 和重复 ID 应在进入渲染前被过滤。"""

    _run_tasks_client(
        r"""
        const overview = client.__normalizeOverview({ all_tasks: [
            null,
            [],
            { title: '没有 ID' },
            { id: ' a/b ', title: ' <任务> ', status: 'cancelled', priority: '2', version: '4' },
            { id: 'a/b', title: '重复项' },
            { id: 2, title: 42, category: {}, plan_date: '2026-02-30' },
        ] });
        assert.equal(overview.all_tasks.length, 2);
        assert.deepEqual(overview.all_tasks[0], {
            id: 'a/b', title: '<任务>', content: '', category: '未分类', status: 'cancelled',
            priority: 2, plan_date: '', deadline_at: '', completed_at: '', cancelled_at: '',
            created_at: '', updated_at: '', version: 4,
        });
        assert.equal(overview.all_tasks[1].id, '2');
        assert.equal(overview.all_tasks[1].title, '');
        assert.equal(overview.all_tasks[1].plan_date, '');
        """
    )


def test_tasks_filters_against_supplied_anchor_and_derives_used_model_only() -> None:
    """周月筛选应跟随显式基准日，派生模型只保留页面实际消费字段。"""

    _run_tasks_client(
        r"""
        const mondayTask = { plan_date: '2024-01-01', status: 'open' };
        assert.equal(client.__planDateMatches(mondayTask, 'week', '2024-01-03'), true);
        assert.equal(client.__planDateMatches({ plan_date: '2024-01-08' }, 'week', '2024-01-03'), false);
        assert.equal(client.__planDateMatches(mondayTask, 'custom', '2024-01-03', 'bad', '2024-01-01'), false);
        assert.equal(client.__planDateMatches(mondayTask, 'custom', '2024-01-03', '2024-01-03', '2023-12-31'), true);
        assert.equal(client.__planDateMatches(mondayTask, 'unknown', '2024-01-03'), false);

        const tasks = client.__normalizeOverview({ all_tasks: [
            { id: 'late', title: '滞后', status: 'open', plan_date: '2026-05-19', priority: 2, category: '工作' },
            { id: 'today', title: '今天', status: 'open', plan_date: '2026-05-20', priority: 1, category: '工作' },
            { id: 'next', title: '近期', status: 'open', plan_date: '2026-05-25', category: '生活' },
            { id: 'later', title: '更晚', status: 'open', plan_date: '2026-06-01' },
            { id: 'backlog', title: '未安排', status: 'open' },
            { id: 'done', title: '完成', status: 'done', completed_at: '2026-05-20T09:00:00' },
            { id: 'cancel', title: '取消', status: 'cancelled', cancelled_at: '2026-05-20T10:00:00' },
        ] }).all_tasks;
        const model = client.__deriveDisplayModel(tasks);
        assert.deepEqual(model.summary, {
            active_count: 5, focus_count: 2, overdue_count: 1, done_count: 1,
            cancelled_count: 1, closed_count: 2, completion_rate: 1 / 6,
        });
        assert.deepEqual(model.focus_tasks.map((task) => task.id), ['late', 'today']);
        assert.deepEqual(model.up_next_tasks.map((task) => task.id), ['next']);
        assert.deepEqual(model.closed_recent.map((task) => task.id), ['cancel', 'done']);
        assert.deepEqual(Object.keys(model.board_columns), ['open', 'closed']);
        assert.equal(model.text_category_count, 2);
        assert.equal(model.match_count, 7);
        assert.equal('category_load' in model, false);
        assert.equal('done_today_count' in model.summary, false);
        assert.equal(model.completion_bars.at(-1).count, 1);
        """
    )


def test_tasks_rendering_escapes_content_and_exposes_keyboard_actions() -> None:
    """任务内容必须转义，列表和看板编辑入口必须是可聚焦按钮。"""

    _run_tasks_client(
        r"""
        const task = client.__normalizeOverview({ all_tasks: [{
            id: 'x/1', title: '<script>x</script>', content: '<img src=x>',
            category: '<分类>', status: 'open', priority: 1,
        }] }).all_tasks[0];
        const row = client.__taskRowHTML(task);
        const card = client.__boardCardHTML(task);
        assert.ok(row.includes('type="button" class="task-row-title"'));
        assert.ok(row.includes('&lt;script&gt;x&lt;/script&gt;'));
        assert.ok(row.includes('&lt;img src=x&gt;'));
        assert.ok(row.includes('&lt;分类&gt;'));
        assert.ok(!row.includes('<script>'));
        assert.ok(card.includes('type="button" class="tasks-board-card-title"'));
        assert.ok(card.includes('data-task-id="x/1"'));

        const root = __makeRoot();
        client.__setTasksTestState({
            container: root,
            overview: { all_tasks: [task] },
            filters: {},
            loading: false,
            loadError: '',
            viewMode: 'list',
            clearPending: true,
        });
        client.__renderPage();
        assert.ok(root.innerHTML.includes('aria-pressed="true"'));
        assert.ok(root.innerHTML.includes('type="button" class="btn btn-primary"'));
        assert.ok(root.innerHTML.includes('1 项匹配'));

        client.__setTasksTestState({ filters: { plan: 'custom', customStart: '2026-05-01', customEnd: '2026-05-31' } });
        client.__renderPage();
        assert.ok(root.innerHTML.includes('pattern="\\d{4}-\\d{2}-\\d{2}"'));
        """
    )


def test_tasks_status_update_encodes_id_uses_version_and_deduplicates() -> None:
    """同一任务的并发状态写入只能发送一次，并携带编码 ID 与乐观版本号。"""

    _run_tasks_client(
        r"""
        const deferred = __deferred();
        __apiHandlers.put = () => deferred.promise;
        const overview = client.__normalizeOverview({ all_tasks: [{
            id: 'a/b ?c', title: '任务', status: 'open', version: 4,
        }] });
        client.__setTasksTestState({ container: null, overview, clearPending: true });

        const first = client.__updateTaskStatus('a/b ?c', 'done');
        const duplicate = client.__updateTaskStatus('a/b ?c', 'cancelled');
        assert.equal(await duplicate, false);
        assert.equal(__apiCalls.length, 1);
        assert.deepEqual(__apiCalls[0], {
            method: 'put', args: ['/items/a%2Fb%20%3Fc', { status: 'done', version: 4 }],
        });
        deferred.resolve({ data: {} });
        assert.equal(await first, true);
        assert.equal(__events.length, 1);
        assert.equal(__events[0].detail.type, 'task');
        assert.deepEqual(client.__getTasksTestState().pendingTaskIds, []);

        assert.equal(await client.__updateTaskStatus('a/b ?c', 'open'), false);
        assert.equal(await client.__updateTaskStatus('', 'done'), false);
        assert.equal(await client.__updateTaskStatus('a/b ?c', 'broken'), false);
        assert.equal(__apiCalls.length, 1);
        """
    )


def test_tasks_modal_blocks_duplicate_save_and_drops_unknown_fields() -> None:
    """保存按钮在请求期间必须锁定，提交载荷不能夹带表单外字段。"""

    _run_tasks_client(
        r"""
        const save = __makeButton();
        const cancel = __makeButton();
        const form = {};
        __modalContent = __makeRoot({ nodes: {
            '#task-save': save,
            '#task-cancel': cancel,
            '#task-form': form,
        } });
        __formData = {
            title: ' 新任务 ', content: ' 内容 ', category: '', status: 'open', priority: '2',
            plan_date: '2026-05-21', deadline_at: '', injected: 'drop-me',
        };
        const deferred = __deferred();
        __apiHandlers.post = () => deferred.promise;

        await client.openTaskModal();
        const first = save.click();
        const duplicate = save.click();
        await duplicate;
        assert.equal(save.disabled, true);
        assert.equal(__apiCalls.length, 1);
        assert.deepEqual(__apiCalls[0], {
            method: 'post',
            args: ['/items', {
                type: 'task', title: '新任务', content: '内容', category: '未分类',
                status: 'open', priority: 2, plan_date: '2026-05-21', deadline_at: null,
            }],
        });
        deferred.resolve({ data: { id: 'new' } });
        await first;
        assert.equal(__closeModalCount, 1);
        assert.equal(__events.length, 1);
        assert.equal(save.disabled, false);
        """
    )


def test_tasks_closed_board_drop_preserves_cancelled_status() -> None:
    """取消任务拖回已结束栏不得被悄悄改成完成，拖回未完成栏才应恢复。"""

    _run_tasks_client(
        r"""
        const makeZone = (column) => ({
            dataset: { col: column },
            listeners: {},
            classList: { add() {}, remove() {} },
            contains() { return false; },
            addEventListener(type, listener) { this.listeners[type] = listener; },
        });
        const closedZone = makeZone('closed');
        const openZone = makeZone('open');
        const root = __makeRoot({ lists: {
            '.tasks-board-card': [],
            '.tasks-board-list': [closedZone, openZone],
        } });
        const overview = client.__normalizeOverview({ all_tasks: [{
            id: 'cancelled', title: '已取消', status: 'cancelled', version: 3,
        }] });
        client.__setTasksTestState({
            container: root, overview, viewMode: 'board', filters: {}, clearPending: true,
        });
        client.__attachListeners();
        const event = {
            preventDefault() {},
            dataTransfer: { getData: () => 'cancelled' },
        };

        await closedZone.listeners.drop(event);
        assert.equal(__apiCalls.length, 0);

        await openZone.listeners.drop(event);
        assert.equal(__apiCalls.length, 1);
        assert.deepEqual(__apiCalls[0], {
            method: 'put', args: ['/items/cancelled', { status: 'open', version: 3 }],
        });
        """
    )


def test_tasks_latest_load_wins_and_destroy_ignores_late_response() -> None:
    """并发刷新必须以后发请求为准，销毁后的响应不得重新写回页面。"""

    _run_tasks_client(
        r"""
        const first = __deferred();
        const second = __deferred();
        let requestIndex = 0;
        __apiHandlers.get = () => (++requestIndex === 1 ? first.promise : second.promise);
        const root = __makeRoot();
        client.__setTasksTestState({
            container: root,
            overview: { all_tasks: [] },
            filters: {},
            loadVersion: 0,
        });

        const firstLoad = client.__loadAndRender();
        const secondLoad = client.__loadAndRender();
        second.resolve({ data: { all_tasks: [{ id: 'newer', title: '新响应' }] } });
        await secondLoad;
        first.resolve({ data: { all_tasks: [{ id: 'older', title: '旧响应' }] } });
        await firstLoad;
        assert.equal(client.__getTasksTestState().overview.all_tasks[0].id, 'newer');
        assert.equal(client.__getTasksTestState().loading, false);

        const late = __deferred();
        __apiHandlers.get = () => late.promise;
        const pending = client.__loadAndRender();
        client.destroy();
        late.resolve({ data: { all_tasks: [{ id: 'late' }] } });
        await pending;
        const state = client.__getTasksTestState();
        assert.equal(state.container, null);
        assert.equal(state.overview, null);
        assert.equal(state.loading, false);
        """
    )


def test_tasks_render_starts_one_request_and_initial_failure_is_retryable() -> None:
    """首次挂载只请求一次；加载失败必须显示转义后的可重试错误态。"""

    _run_tasks_client(
        r"""
        __apiHandlers.get = async () => { throw new Error('<network>'); };
        const retry = __makeButton();
        const root = __makeRoot({ nodes: { '#tasks-retry': retry } });
        client.render(root);
        await __flushPromises();
        assert.equal(__apiCalls.filter((call) => call.method === 'get').length, 1);
        assert.equal(__subscriptions.length, 1);
        assert.equal(__subscriptions[0].type, 'task');
        assert.ok(root.innerHTML.includes('role="alert"'));
        assert.ok(root.innerHTML.includes('&lt;network&gt;'));
        assert.ok(!root.innerHTML.includes('<network>'));
        assert.equal(__toastCalls.length, 1);
        assert.equal(typeof retry._listeners.click, 'function');

        client.destroy();
        assert.equal(__unsubscribeCount, 1);
        """
    )
