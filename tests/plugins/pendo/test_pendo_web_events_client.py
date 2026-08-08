"""Pendo Web 日程页的数据边界、编辑器与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_timezone_test_support import inline_timezone_runtime

ROOT: Final = REPOSITORY_ROOT
EVENTS_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "events.js"
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)

EVENTS_SETUP: Final = r"""
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
    globalThis.__styleCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__dispatchedEvents = [];
    globalThis.__scrollCalls = [];
    globalThis.__selectHandlers = null;
    globalThis.__fetchItemRangeBounds = async () => ({
        start: '2020-01-01', end: '2026-03-15',
    });
    globalThis.__subscribeDataChanges = (_type, callback) => {
        __dataChangeCallback = callback;
        return () => { __unsubscribeCount += 1; };
    };
    globalThis.window = {
        scrollY: 0,
        dispatchEvent(event) { __dispatchedEvents.push(event); return true; },
        requestAnimationFrame(callback) { callback(); return 1; },
        scrollTo(options) { __scrollCalls.push(options); },
    };
    globalThis.CustomEvent = class {
        constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    };
"""


def _events_source_for_test() -> str:
    """替换浏览器相邻依赖，并嵌入真实共享日期实现。"""

    source = EVENTS_CLIENT.read_text(encoding="utf-8")
    timezone_runtime = inline_timezone_runtime(TIMEZONE_CLIENT)
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{ formatSharedDateTime, isoDate, isValidDateInput, pad2, parseDate }} = (() => {{
{format_source}
    return {{
        formatSharedDateTime: formatDateTime,
        isoDate,
        isValidDateInput,
        pad2,
        parseDate,
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
            "import { renderCustomSelect, initCustomSelects } "
            "from '../components/custom_select.js';",
            """const renderCustomSelect = () => '<div class="stub-select"></div>';
const initCustomSelects = (_root, handlers) => { globalThis.__selectHandlers = handlers; };""",
        ),
        (
            "import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, "
            "todayRangeKey } from '../utils/date_ranges.js';",
            """const RANGE_PRESET_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'custom', label: '自定义' },
    { key: 'all', label: '全部' },
];
const derivePresetRange = (_key, options) => ({
    start: options.customStart || '2026-03-01',
    end: options.customEnd || options.today,
});
const fetchItemRangeBounds = (...args) => globalThis.__fetchItemRangeBounds(...args);
const todayRangeKey = () => '2026-03-15';""",
        ),
        (
            "import { formatDateTime as formatSharedDateTime, isoDate, "
            "isValidDateInput, pad2, parseDate } from '../utils/format.js';",
            format_runtime,
        ),
        (
            "import { fetchUserTimeZone, zonedDateTimeToInput, zonedInputToUtcIso } "
            "from '../utils/timezone.js';",
            timezone_runtime,
        ),
        (
            """import {
    bindEnterAction,
    bindFormSubmit,
    BREAKPOINTS,
    escapeHtml,
    injectStyles,
    mediaMax,
    pageShellCss,
    subscribeDataChanges,
} from '../utils/ui.js';""",
            """const bindEnterAction = (element, action) => {
    if (!element || typeof action !== 'function') return;
    element.onkeydown = async (event) => {
        if (event.key === 'Enter' && !event.isComposing) {
            event.preventDefault();
            await action();
        }
    };
};
const bindFormSubmit = (form, submitButton) => {
    if (!form || !submitButton) return;
    form.onsubmit = (event) => {
        event.preventDefault();
        submitButton.click();
    };
};
const BREAKPOINTS = {
    XL: '1200px', MOBILE: '720px', PHONE: '560px',
};
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (_breakpoint, cssText) => cssText;
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
    collectEditorPayload,
    deleteCollection as __deleteCollection,
    deleteEvent as __deleteEvent,
    editorModalHTML,
    formatEventDateTime,
    inputToIso,
    attachPageListeners as __attachPageListeners,
    loadOverview as __loadOverview,
    normalizeOverview,
    openEventEditor as __openEventEditor,
    reminderRulesFromTimes,
    renderCalendarCell,
    renderDetailBody,
    renderTimelineEntries,
    toInputDateTime,
};
export function __setEventsTestState(state = {}) {
    _state = {
        ..._state,
        ...state,
        filters: { ..._state.filters, ...(state.filters || {}) },
    };
}

export function __setEventsTestContainer(container) {
    _container = container;
}
"""
    )


def _run_events_client(script: str) -> None:
    """在 Node 中执行日程页真实 ESM 数据、渲染、表单和生命周期。"""

    assert_node_esm_contract(
        _events_source_for_test(),
        script,
        cwd=ROOT,
        setup=EVENTS_SETUP,
    )


def test_events_dates_rules_and_overview_normalization() -> None:
    """非法日期不得滚动，提醒偏移与接口异常字段应稳定收敛。"""

    _run_events_client(
        r"""
        assert.equal(client.inputToIso('2026-03-02 09:05', 'Asia/Shanghai'), '2026-03-02T01:05:00+00:00');
        assert.equal(client.inputToIso('2026-03-02T09:05:07', 'Asia/Shanghai'), '2026-03-02T01:05:07+00:00');
        assert.equal(client.inputToIso('2026-02-30T09:05', 'Asia/Shanghai'), '');
        assert.equal(client.inputToIso('2026-03-02T24:00', 'Asia/Shanghai'), '');
        assert.equal(client.toInputDateTime('2026-02-30T09:05:00', 'Asia/Shanghai'), '');
        assert.equal(client.toInputDateTime('2026-03-02T01:05:00+00:00', 'Asia/Shanghai'), '2026-03-02T09:05');
        assert.equal(client.formatEventDateTime('2026-02-30T09:05:00'), '未知时间');
        assert.deepEqual(
            client.reminderRulesFromTimes(
                '2026-03-02T09:00:00',
                ['2026-03-02T08:00:00', 'bad', '2026-03-02T10:00:00'],
            ),
            [{ offset_seconds: 3600 }],
        );
        assert.deepEqual(
            client.reminderRulesFromTimes('2026-03-02T09:00:00', []),
            [],
        );
        assert.ok(client.editorModalHTML(null, '2026-03-02', 'Asia/Shanghai').includes('data-reminder-row'));
        assert.ok(!client.editorModalHTML({
            start_time: '2026-03-02T09:00:00', reminders: [],
        }, '', 'Asia/Shanghai').includes('data-reminder-row'));

        const normalized = client.normalizeOverview({
            summary: {
                event_count: '<script>',
                multi_node_count: 2.9,
                reminder_count: Number.POSITIVE_INFINITY,
            },
            categories: ['会议', '会议', '', null, '<script>'],
            calendar_days: [],
            timeline_days: [
                { date: '2026-03-02', items: 'bad' },
                { date: '2026-02-30', items: [{}] },
                null,
            ],
            events: [null, { id: 'event-1' }, 'bad'],
        });
        assert.deepEqual(normalized.summary, {
            event_count: 0, multi_node_count: 2, reminder_count: 0,
        });
        assert.deepEqual(normalized.categories, ['会议', '<script>']);
        assert.deepEqual(normalized.calendar_days, {});
        assert.deepEqual(normalized.timeline_days, [{ date: '2026-03-02', items: [] }]);
        assert.deepEqual(normalized.events, [{ id: 'event-1' }]);
        """
    )


def test_events_renderers_escape_values_and_use_native_controls() -> None:
    """日历、时间线和详情不得泄漏标记或把异常枚举拼进 class。"""

    _run_events_client(
        r"""
        const hostileId = 'event/&quot;><script>';
        client.__setEventsTestState({
            selectedDate: '2026-03-02',
            overview: {
                summary: { event_count: 1, multi_node_count: 0, reminder_count: 0 },
                categories: [],
                calendar_days: {},
                timeline_days: [],
                events: [{
                    id: hostileId,
                    kind: '"><script>kind</script>',
                    title: '<img src=x onerror=alert(1)>',
                    category: '<script>category</script>',
                }],
            },
        });
        const calendar = client.renderCalendarCell(
            new Date(2026, 2, 2),
            2,
            {
                count: Number.POSITIVE_INFINITY,
                items: [{
                    event_id: hostileId,
                    kind: '"><script>kind</script>',
                    label: '<img src=x onerror=alert(1)>',
                }],
            },
            '2026-03-02',
        );
        assert.match(calendar, /<button type="button" class="events-calendar-head"/);
        assert.match(calendar, /aria-pressed="true"/);
        assert.match(calendar, /events-calendar-chip single/);
        assert.ok(calendar.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(!calendar.includes('<script>'));
        assert.ok(!calendar.includes('Infinity'));

        const timeline = client.renderTimelineEntries([{
            event_id: hostileId,
            kind: '"><script>kind</script>',
            title: '<svg onload=alert(1)>',
            location: '<script>place</script>',
            reminder_total: Number.NaN,
        }]);
        assert.match(timeline, /<button type="button" class="events-timeline-card"/);
        assert.ok(timeline.includes('kind-single'));
        assert.ok(timeline.includes('&lt;script&gt;place&lt;/script&gt;'));
        assert.ok(!timeline.includes('<script>'));
        assert.ok(!timeline.includes('NaN'));

        const detail = client.renderDetailBody({
            event: {
                title: '<script>title</script>',
                notes: '<img src=x onerror=alert(1)>',
                start_time: '2026-02-30T09:00:00',
                reminders: [{
                    time: 'bad', status: '"><script>status</script>',
                    repeat_count: Number.POSITIVE_INFINITY,
                }],
            },
            related_instances: [{
                title: '<b>next</b>', start_time: '2026-02-30T09:00:00',
            }],
        });
        assert.ok(detail.includes('未知时间'));
        assert.ok(detail.includes('events-status pending'));
        assert.ok(detail.includes('&lt;script&gt;title&lt;/script&gt;'));
        assert.ok(detail.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(!detail.includes('style="'));
        assert.ok(!detail.includes('<script>'));
        assert.ok(!detail.includes('Infinity'));
        """
    )


def test_events_filter_handlers_restrict_values_before_request() -> None:
    """筛选控件值必须在进入概览请求前收敛到接口白名单。"""

    _run_events_client(
        r"""
        const calls = [];
        const emptyOverview = {
            summary: { event_count: 0, multi_node_count: 0, reminder_count: 0 },
            categories: ['工作'],
            calendar_days: {},
            timeline_days: [],
            events: [],
        };
        __api.get = async (_path, params) => {
            calls.push(params);
            return { data: emptyOverview };
        };
        const root = {
            innerHTML: '',
            onclick: null,
            querySelector: () => null,
            querySelectorAll: () => [],
        };
        client.__setEventsTestState({ overview: emptyOverview });
        client.__setEventsTestContainer(root);
        client.__attachPageListeners(root);

        await __selectHandlers['events-filter-category']('<script>');
        await __selectHandlers['events-filter-kind']('<script>');
        await __selectHandlers['events-filter-reminder']('<script>');
        assert.equal(calls[0].category, '');
        assert.equal(calls[1].kind, 'all');
        assert.equal(calls[2].reminder, 'all');

        await __selectHandlers['events-filter-category']('工作');
        await __selectHandlers['events-filter-kind']('recurring');
        await __selectHandlers['events-filter-reminder']('with');
        assert.equal(calls[3].category, '工作');
        assert.equal(calls[4].kind, 'recurring');
        assert.equal(calls[5].reminder, 'with');
        """
    )


def test_events_editor_validates_single_and_multi_node_payloads() -> None:
    """表单应拒绝残缺节点、倒置时间和晚于事件的提醒。"""

    _run_events_client(
        r"""
        const field = (value = '') => ({ value });
        const fields = new Map([
            ['[name="title"]', field('产品评审')],
            ['[name="category"]', field('会议')],
            ['[name="location"]', field('A1')],
            ['[name="notes"]', field('材料')],
            ['[name="start_time"]', field('2026-03-02T09:00')],
            ['[name="end_time"]', field('2026-03-02T10:00')],
        ]);
        const reminders = [field('2026-03-02T08:00'), field('2026-03-02T08:00')];
        const form = {
            dataset: { mode: 'single' },
            querySelector: (selector) => fields.get(selector) || null,
            querySelectorAll(selector) {
                if (selector === '.events-editor-reminder-input') return reminders;
                if (selector === '[data-node-row]') return [];
                return [];
            },
        };
        const content = {
            querySelector: (selector) => selector === '#events-editor-form' ? form : null,
        };
        assert.deepEqual(client.collectEditorPayload(content, 'Asia/Shanghai'), {
            title: '产品评审',
            category: '会议',
            location: 'A1',
            notes: '材料',
            timezone: 'Asia/Shanghai',
            start_time: '2026-03-02T01:00:00+00:00',
            end_time: '2026-03-02T02:00:00+00:00',
            reminder_rules: [{ offset_seconds: 3600 }],
        });

        fields.get('[name="end_time"]').value = '2026-03-02T08:30';
        assert.throws(() => client.collectEditorPayload(content, 'Asia/Shanghai'), /结束时间不能早于开始时间/);
        fields.get('[name="end_time"]').value = '2026-03-02T10:00';
        reminders[0].value = '2026-03-02T09:30';
        reminders[1].value = '';
        assert.throws(() => client.collectEditorPayload(content, 'Asia/Shanghai'), /提醒时间不能晚于开始时间/);

        const nodeRow = (name, time, notes = '') => ({
            querySelector(selector) {
                if (selector === '.events-editor-node-name') return field(name);
                if (selector === '.events-editor-node-time') return field(time);
                if (selector === '.events-editor-node-notes') return field(notes);
                return null;
            },
        });
        form.dataset.mode = 'multi_node';
        reminders[0].value = '2026-03-02T07:00';
        form.querySelectorAll = (selector) => {
            if (selector === '.events-editor-reminder-input') return [reminders[0]];
            if (selector === '[data-node-row]') return [
                nodeRow('上线', '2026-03-03T18:00', '发布'),
                nodeRow('提审', '2026-03-02T09:00'),
            ];
            return [];
        };
        const multi = client.collectEditorPayload(content, 'Asia/Shanghai');
        assert.equal(multi.kind, 'multi_node');
        assert.deepEqual(multi.children, [
            { title: '提审', start_time: '2026-03-02T01:00:00+00:00' },
            { title: '上线', start_time: '2026-03-03T10:00:00+00:00', notes: '发布' },
        ]);
        assert.deepEqual(multi.reminder_rules, [{ offset_seconds: 7200 }]);

        form.querySelectorAll = (selector) => selector === '[data-node-row]'
            ? [nodeRow('', '2026-03-02T09:00'), nodeRow('上线', '2026-03-03T18:00')]
            : [];
        assert.throws(() => client.collectEditorPayload(content, 'Asia/Shanghai'), /请填写节点名称/);
        """
    )


def test_events_loads_ignore_stale_and_destroyed_responses() -> None:
    """筛选刷新时后发请求应胜出，销毁后的慢响应不得覆盖其他页面。"""

    _run_events_client(
        r"""
        const requests = [];
        __api.get = (path, params = {}) => new Promise((resolve, reject) => {
            requests.push({ path, params, resolve, reject });
        });
        const container = {
            innerHTML: '',
            querySelector: () => null,
            querySelectorAll: () => [],
        };
        assert.throws(() => client.render(null), /日程页需要有效的 DOM 挂载容器/);
        client.render(container);
        assert.equal(requests.length, 1);
        assert.match(container.innerHTML, /加载中/);

        __dataChangeCallback();
        assert.equal(requests.length, 2);
        requests[1].resolve({
            data: {
                summary: { event_count: 2 },
                calendar_days: { '2026-03-01': { items: [], count: 0 } },
            },
        });
        await __flushPromises();
        const newestMarkup = container.innerHTML;
        assert.match(newestMarkup, /2 条日程/);

        requests[0].resolve({ data: { summary: { event_count: 99 } } });
        await __flushPromises();
        assert.equal(container.innerHTML, newestMarkup);
        client.destroy();
        assert.equal(__unsubscribeCount, 1);

        client.render(container);
        assert.equal(requests.length, 3);
        client.destroy();
        container.innerHTML = '<main>其他页面</main>';
        requests[2].resolve({ data: { summary: { event_count: 88 } } });
        await __flushPromises();
        assert.equal(container.innerHTML, '<main>其他页面</main>');
        assert.equal(__unsubscribeCount, 2);
        """
    )


def test_events_mutations_encode_ids_block_duplicates_and_dispatch_once() -> None:
    """保存与删除应编码路径、防重复提交，并只派发一次类型化刷新事件。"""

    _run_events_client(
        r"""
        const field = (value = '') => ({ value });
        const fields = new Map([
            ['[name="title"]', field('产品评审')],
            ['[name="category"]', field('会议')],
            ['[name="location"]', field('A1')],
            ['[name="notes"]', field('材料')],
            ['[name="start_time"]', field('2026-03-02T09:00')],
            ['[name="end_time"]', field('2026-03-02T10:00')],
        ]);
        const form = {
            dataset: { mode: 'single' },
            querySelector: (selector) => fields.get(selector) || null,
            querySelectorAll: (selector) => selector === '.events-editor-reminder-input'
                ? [field('2026-03-02T08:00')] : [],
        };
        const buttons = new Map([
            ['#events-editor-cancel', { onclick: null }],
            ['#events-add-reminder', { onclick: null }],
            ['#events-editor-save', { onclick: null, disabled: false }],
        ]);
        __modalContent = {
            querySelector(selector) {
                if (selector === '#events-editor-form') return form;
                if (selector === '#events-editor-reminders') {
                    return { insertAdjacentHTML() {} };
                }
                return buttons.get(selector) || null;
            },
            querySelectorAll: () => [],
            addEventListener() {},
        };

        const putCalls = [];
        let resolvePut;
        __api.put = (...args) => {
            putCalls.push(args);
            return new Promise((resolve) => { resolvePut = resolve; });
        };
        await client.__openEventEditor({
            id: 'event/a b', title: '产品评审', start_time: '2026-03-02T09:00:00',
        });
        const saveButton = buttons.get('#events-editor-save');
        const firstSave = saveButton.onclick();
        const duplicateSave = saveButton.onclick();
        assert.equal(putCalls.length, 1);
        assert.equal(saveButton.disabled, true);
        resolvePut({ data: {} });
        await Promise.all([firstSave, duplicateSave]);
        assert.deepEqual(putCalls, [[
            '/items/event%2Fa%20b',
            {
                title: '产品评审', category: '会议', location: 'A1', notes: '材料',
                timezone: 'Asia/Shanghai',
                start_time: '2026-03-02T01:00:00+00:00',
                end_time: '2026-03-02T02:00:00+00:00',
                reminder_rules: [{ offset_seconds: 3600 }],
            },
        ]]);
        assert.equal(saveButton.disabled, false);
        assert.equal(__dispatchedEvents.length, 1);
        assert.deepEqual(__dispatchedEvents[0].detail, { type: 'event' });
        assert.ok(__modalCalls[0][2].footer.includes('type="button"'));

        const deleteCalls = [];
        __api.delete = async (...args) => { deleteCalls.push(args); };
        await client.__deleteEvent('event/a b', '产品评审');
        await client.__deleteCollection('collection/a b', '发布项目');
        assert.deepEqual(deleteCalls, [
            ['/items/event%2Fa%20b'],
            ['/events/collections/collection%2Fa%20b'],
        ]);
        assert.equal(__dispatchedEvents.length, 3);
        for (const event of __dispatchedEvents) {
            assert.deepEqual(event.detail, { type: 'event' });
        }
        """
    )
