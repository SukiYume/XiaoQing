"""Pendo Web 日记页的数据边界、渲染、表单与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_timezone_test_support import inline_timezone_runtime

ROOT: Final = REPOSITORY_ROOT
DIARY_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js"
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)

DIARY_SETUP: Final = r"""
    globalThis.__api = {
        get: async () => ({ data: {} }),
        post: async () => ({ data: {} }),
        put: async () => ({ data: {} }),
        delete: async () => ({ data: {} }),
    };
    globalThis.__toastCalls = [];
    globalThis.__modalCalls = [];
    globalThis.__closeModalCount = 0;
    globalThis.__confirmResult = true;
    globalThis.__modalContent = null;
    globalThis.__formData = {};
    globalThis.__styleCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__dispatchedEvents = [];
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
"""


def _diary_source_for_test() -> str:
    """替换浏览器相邻依赖，并嵌入真实共享格式化实现。"""

    source = DIARY_CLIENT.read_text(encoding="utf-8")
    timezone_runtime = inline_timezone_runtime(TIMEZONE_CLIENT)
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{
    errorMessage,
    finiteNumber,
    isRecord,
    isoDate,
    nonNegativeInteger,
    pad2,
    parseDate,
    previewText,
    records,
}} = (() => {{
{format_source}
    return {{
        errorMessage,
        finiteNumber,
        isRecord,
        isoDate,
        nonNegativeInteger,
        pad2,
        parseDate,
        previewText,
        records,
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
            """const buildFormHTML = () => '<div class="stub-fields"></div>';
const getFormData = () => ({ ...globalThis.__formData });
const initFormInteractions = () => {};""",
        ),
        (
            "import { renderCustomSelect, initCustomSelects } "
            "from '../components/custom_select.js';",
            """const renderCustomSelect = () => '<div class="stub-select"></div>';
const initCustomSelects = () => {};""",
        ),
        (
            """import {
    errorMessage,
    finiteNumber,
    isRecord,
    isoDate,
    nonNegativeInteger,
    pad2,
    parseDate,
    previewText,
    records,
} from '../utils/format.js';""",
            format_runtime,
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
const mediaMax = (_breakpoint, cssText) => cssText;
const pageShellCss = () => '';
const subscribeDataChanges = (...args) => globalThis.__subscribeDataChanges(...args);""",
        ),
        (
            """import {
    fetchUserTimeZone,
    todayInUserTimeZone,
    zonedDateKey,
    zonedDateParts,
    zonedDateTimeToInput,
    zonedInputToUtcIso,
} from '../utils/timezone.js';""",
            timezone_runtime,
        ),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    buildItemsByDate,
    compactDiaryCellLabel,
    deleteDiary as __deleteDiary,
    diaryWordCount,
    ensureStyles as __ensureStyles,
    fetchItems,
    formatEntryTime,
    formatWordMetric,
    normalizeDiaryOverview,
    normalizeTemplateAnswers,
    openDiaryFormModal as __openDiaryFormModal,
    renderCadencePanel,
    renderCalendarPanel,
    renderHero,
    renderMoodPanel,
    renderSelectedDay,
    templateAnswerInputRows,
    templateAnswersToContent,
    updateMonth,
};
export function __setDiaryTestState(state = {}) {
    _year = state.year ?? 2026;
    _month = state.month ?? 3;
    _selectedDate = state.selectedDate ?? '';
    _items = sortItems(state.items ?? []);
    _overview = state.overview === null
        ? null
        : normalizeDiaryOverview(state.overview ?? {});
    _loading = Boolean(state.loading);
}
"""
    )


def _run_diary_client(script: str) -> None:
    """在 Node 中执行日记页真实 ESM 数据、DOM 字符串与生命周期。"""

    assert_node_esm_contract(
        _diary_source_for_test(),
        script,
        cwd   = ROOT,
        setup = DIARY_SETUP,
    )


def test_diary_helpers_normalize_unicode_dates_metrics_and_template_rows() -> None:
    """字数应按 Unicode 字符计数，日期、统计和历史模板回答都应稳定收敛。"""

    _run_diary_client(
        r"""
        assert.equal(client.diaryWordCount({ content: ' 🙂a ' }), 2);
        assert.equal(client.compactDiaryCellLabel({ content: '  🙂  一天  ' }, 3), '🙂 一…');
        assert.equal(client.formatWordMetric(Symbol('bad')), '0');
        assert.equal(client.formatWordMetric(Number.POSITIVE_INFINITY), '0');
        assert.equal(client.formatWordMetric(1250), '1.3k');
        assert.equal(client.formatEntryTime({ diary_date: '2026-02-30' }), '未知日期');
        assert.equal(
            client.formatEntryTime({
                diary_date: '2026-03-02', entry_time: '2026-03-02T08:05:00',
            }),
            '2026-03-02 08:05',
        );
        assert.equal(
            client.formatEntryTime({ created_at: '2026-05-01T16:30:00+00:00' }),
            '2026-05-02 00:30',
        );

        const overview = client.normalizeDiaryOverview({
            summary: {
                entry_count: '<script>',
                active_days: -3,
                fill_rate: 8,
                total_words: Number.POSITIVE_INFINITY,
                busiest_day: { date: 9, words: -1 },
            },
            mood_breakdown: [{ mood: 'happy', count: 2.9, share: 7 }],
            cadence: [{ date: 3, label: 4, words: Symbol('bad') }],
        });
        assert.equal(overview.summary.entry_count, 0);
        assert.equal(overview.summary.active_days, 0);
        assert.equal(overview.summary.fill_rate, 1);
        assert.equal(overview.summary.total_words, 0);
        assert.deepEqual(overview.summary.busiest_day, { date: '', words: 0 });
        assert.deepEqual(overview.mood_breakdown[0], {
            mood: 'happy', count: 2, share: 1,
        });
        assert.deepEqual(overview.cadence[0], { date: '', label: '', words: 0 });

        const rows = client.templateAnswerInputRows(
            { prompts: ['问题 A', '问题 A', '问题 B'] },
            [
                { prompt: '问题 A', answer: '回答 A' },
                { prompt: '', answer: '无题回答' },
                { prompt: '旧问题', answer: '旧回答' },
            ],
        );
        assert.deepEqual(rows, [
            { prompt: '问题 A', answer: '回答 A', label: '问题 A' },
            { prompt: '问题 B', answer: '', label: '问题 B' },
            { prompt: '', answer: '无题回答', label: '问题 3' },
            { prompt: '旧问题', answer: '旧回答', label: '旧问题' },
        ]);
        assert.equal(
            client.templateAnswersToContent(rows),
            '问题 A\n回答 A\n\n无题回答\n\n旧问题\n旧回答',
        );
        """
    )


def test_diary_mobile_calendar_stays_within_panel_and_uses_compact_entry_marks() -> None:
    """手机月历必须允许七列收缩，并用紧凑色条呈现有内容的日期。"""

    _run_diary_client(
        r"""
        client.__ensureStyles();
        const css = __styleCalls.at(-1)[1];

        assert.match(css, /\.diary-summary-card, \.diary-panel, \.diary-workspace \{[\s\S]*?min-width: 0; overflow: hidden;/);
        assert.match(css, /\.diary-layout \{[\s\S]*?min-width: 0;/);
        assert.match(css, /\.diary-calendar-weekdays, \.diary-calendar-grid \{[\s\S]*?width: 100%; min-width: 0;/);
        assert.ok(css.includes('.diary-layout { grid-template-columns: minmax(0, 1fr); }'));
        assert.match(css, /\.diary-day \{ width: 100%; max-width: 100%; min-height: 78px;/);
        assert.match(css, /\.diary-day-copy \{[\s\S]*?height: 6px;[\s\S]*?font-size: 0;/);
        """
    )


def test_diary_renderers_escape_analytics_and_build_accessible_calendar() -> None:
    """日历与统计图不得泄漏异常标记，并应给日期按钮完整的可访问状态。"""

    _run_diary_client(
        r"""
        const items = [{
            id: 'd1',
            diary_date: '2026-03-02',
            entry_time: '2026-03-02T08:00:00',
            title: '<img src=x onerror=alert(1)>',
            content: '🙂',
            mood: '"><script>alert(1)</script>',
        }];
        const rawOverview = {
            summary: {
                entry_count: 1,
                active_days: 1,
                current_streak: 1,
                busiest_day: { date: '"><script>bad</script>', words: 1 },
            },
            mood_breakdown: [{
                mood: '"><script>mood</script>', count: 1, share: 9,
            }],
            cadence: [{
                date: '"><img src=x>', label: '<b>标签</b>', words: Number.POSITIVE_INFINITY,
            }],
        };
        client.__setDiaryTestState({
            year: 2026,
            month: 3,
            selectedDate: '2026-03-02',
            items,
            overview: rawOverview,
        });

        const grouped = client.buildItemsByDate([
            ...items,
            { diary_date: '2026-02-30', content: 'invalid' },
            null,
        ]);
        assert.equal(grouped.size, 1);
        const calendar = client.renderCalendarPanel(grouped);
        assert.match(calendar, /aria-label="2026年3月2日 · 周一，1 字"/);
        assert.match(calendar, /data-date="2026-03-02"[\s\S]*aria-pressed="true"/);
        assert.ok(calendar.includes('1 字 🙂'));
        assert.ok(calendar.includes('&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;'));
        assert.ok(!calendar.includes('<script>'));

        const hero = client.renderHero();
        const moods = client.renderMoodPanel();
        const cadence = client.renderCadencePanel();
        const selected = client.renderSelectedDay(grouped);
        assert.ok(hero.includes('最密集的一天：未知日期 · 1 字'));
        assert.ok(moods.includes('style="width:100%;'));
        assert.ok(moods.includes('&quot;&gt;&lt;script&gt;mood&lt;/script&gt;'));
        assert.ok(cadence.includes('&quot;&gt;&lt;img src=x&gt;'));
        assert.ok(cadence.includes('&lt;b&gt;标签&lt;/b&gt;'));
        assert.ok(!cadence.includes('NaN'));
        assert.ok(!cadence.includes('Infinity'));
        assert.ok(selected.includes('&lt;img src=x onerror=alert(1)&gt;'));
        """
    )


def test_diary_item_fetch_uses_total_and_stops_on_malformed_pages() -> None:
    """月内全量读取应按服务端 total 收口，损坏页不得造成无限请求。"""

    _run_diary_client(
        r"""
        const calls = [];
        const firstPage = Array.from({ length: 80 }, (_, index) => ({
            id: `d${index}`,
            diary_date: '2026-03-01',
            entry_time: `2026-03-01T${String(index % 24).padStart(2, '0')}:00:00`,
        }));
        const secondPage = Array.from({ length: 5 }, (_, index) => ({
            id: `tail${index}`,
            diary_date: '2026-03-02',
            entry_time: `2026-03-02T0${index}:00:00`,
        }));
        __api.get = async (path, params) => {
            assert.equal(path, '/items');
            calls.push(params);
            return {
                data: {
                    items: params.page === 1 ? firstPage : secondPage,
                    total: 85,
                },
            };
        };
        const items = await client.fetchItems(2026, 3);
        assert.equal(calls.length, 2);
        assert.equal(items.length, 85);
        assert.equal(items[0].id, 'tail4');
        assert.equal(calls[0].start_date, '2026-03-01');
        assert.equal(calls[0].end_date, '2026-03-31');

        let malformedCalls = 0;
        __api.get = async () => {
            malformedCalls += 1;
            return { data: { items: 'not-an-array', total: 999 } };
        };
        assert.deepEqual(await client.fetchItems(2026, 3), []);
        assert.equal(malformedCalls, 1);
        """
    )


def test_diary_month_loads_ignore_stale_and_destroyed_responses() -> None:
    """切月时后发请求应胜出，销毁后的旧响应不得覆盖其他页面。"""

    _run_diary_client(
        r"""
        const requests = [];
        __api.get = (path, params = {}) => {
            if (path === '/config/diary/moods') {
                return Promise.resolve({ data: { moods: [] } });
            }
            return new Promise((resolve, reject) => {
                requests.push({ path, params, resolve, reject });
            });
        };
        const container = {
            innerHTML: '',
            querySelector: () => null,
            querySelectorAll: () => [],
        };
        assert.throws(() => client.render(null), /日记页需要有效的 DOM 挂载容器/);
        client.render(container);
        assert.equal(requests.length, 2);

        const monthChange = client.updateMonth(2026, 3);
        assert.equal(requests.length, 4);
        const currentOverview = requests.find((request) =>
            request.path === '/stats/diary/overview'
            && request.params.year === 2026
            && request.params.month === 3
        );
        const currentItems = requests.find((request) =>
            request.path === '/items'
            && request.params.start_date === '2026-03-01'
        );
        currentOverview.resolve({ data: { summary: { entry_count: 3 } } });
        currentItems.resolve({ data: { items: [], total: 0 } });
        await monthChange;
        assert.match(container.innerHTML, /3 篇记录/);
        const newestMarkup = container.innerHTML;

        for (const request of requests.slice(0, 2)) {
            if (request.path === '/items') {
                request.resolve({ data: { items: [], total: 0 } });
            } else {
                request.resolve({ data: { summary: { entry_count: 99 } } });
            }
        }
        await __flushPromises();
        assert.equal(container.innerHTML, newestMarkup);
        client.destroy();
        assert.equal(__unsubscribeCount, 1);

        const requestStart = requests.length;
        client.render(container);
        const destroyedRequests = requests.slice(requestStart);
        client.destroy();
        container.innerHTML = '<main>其他页面</main>';
        for (const request of destroyedRequests) {
            if (request.path === '/items') {
                request.resolve({ data: { items: [], total: 0 } });
            } else {
                request.resolve({ data: { summary: { entry_count: 88 } } });
            }
        }
        await __flushPromises();
        assert.equal(container.innerHTML, '<main>其他页面</main>');
        assert.equal(__unsubscribeCount, 2);
        """
    )


def test_diary_modal_encodes_mutations_and_dispatches_one_refresh() -> None:
    """查看内容应安全，保存需防重复，保存和删除都只派发一次刷新事件。"""

    _run_diary_client(
        r"""
        const buttons = new Map();
        for (const id of [
            '#diary-view-close', '#diary-view-edit', '#diary-view-delete',
            '#diary-modal-cancel', '#diary-modal-delete', '#diary-modal-save',
        ]) {
            buttons.set(id, { disabled: false, onclick: null });
        }
        const form = { querySelectorAll: () => [] };
        __modalContent = {
            querySelector(selector) {
                if (selector === '#diary-form') return form;
                return buttons.get(selector) || null;
            },
        };

        assert.throws(() => client.openDiaryViewModal(null), /查看日记需要有效的条目/);
        client.openDiaryViewModal({
            id: 'view',
            diary_date: '2026-03-02',
            title: '<script>title</script>',
            content: '<img src=x onerror=alert(1)>',
            mood_score: Number.POSITIVE_INFINITY,
        });
        const [, viewBody, viewOptions] = __modalCalls.at(-1);
        assert.ok(viewBody.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(!viewBody.includes('<img src=x'));
        assert.ok(!viewBody.includes('Infinity/10'));
        assert.ok(viewOptions.footer.includes('type="button"'));
        assert.ok(viewOptions.footer.includes('diary-delete-action'));

        const getCalls = [];
        __api.get = async (path) => {
            getCalls.push(path);
            return { data: { templates: [] } };
        };
        const putCalls = [];
        let resolvePut;
        __api.put = (...args) => {
            putCalls.push(args);
            return new Promise((resolve) => { resolvePut = resolve; });
        };
        __formData = {
            diary_date: '2026-03-02',
            entry_time: '2026-03-02T08:30',
            content: '正文',
        };
        await client.__openDiaryFormModal({
            id: 'entry/a b', version: 3, diary_date: '2026-03-02', content: '旧正文',
        });
        const saveButton = buttons.get('#diary-modal-save');
        const saving = saveButton.onclick();
        const duplicate = saveButton.onclick();
        assert.equal(putCalls.length, 1);
        assert.equal(saveButton.disabled, true);
        resolvePut({ data: {} });
        await Promise.all([saving, duplicate]);

        assert.equal(getCalls.length, 1);
        assert.deepEqual(putCalls[0], [
            '/items/entry%2Fa%20b',
            {
                diary_date: '2026-03-02',
                entry_time: '2026-03-02T00:30:00+00:00',
                content: '正文', version: 3,
            },
        ]);
        assert.equal(saveButton.disabled, false);
        assert.deepEqual(__toastCalls.at(-1), ['日记已更新', 'success']);
        assert.equal(__dispatchedEvents.length, 1);
        assert.deepEqual(__dispatchedEvents[0].detail, { type: 'diary' });

        const deleteCalls = [];
        __api.delete = async (...args) => { deleteCalls.push(args); };
        await client.__deleteDiary({ id: 'entry/a b', version: 3, title: '正文' });
        assert.deepEqual(deleteCalls, [['/items/entry%2Fa%20b']]);
        assert.equal(__dispatchedEvents.length, 2);
        assert.equal(getCalls.length, 1);
        """
    )
