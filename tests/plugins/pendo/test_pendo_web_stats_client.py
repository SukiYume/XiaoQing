"""Pendo Web 统计页的图表边界、请求契约与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
STATS_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js"

STATS_SETUP: Final = r"""
    globalThis.__apiCalls = [];
    globalThis.__api = {
        get: async (path, params = {}) => {
            __apiCalls.push({ path, params });
            return { data: {} };
        },
    };
    globalThis.__toastCalls = [];
    globalThis.__styleCalls = [];
    globalThis.__boundsCalls = [];
    globalThis.__fetchItemRangeBounds = async (_api, options) => {
        __boundsCalls.push(options);
        return { start: '2020-01-01', end: options.fallbackEnd };
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
        value: '',
        dataset: {},
        onclick: null,
        oninput: null,
        onkeydown: null,
        ...extra,
    });
    globalThis.__makeRoot = ({ nodes = {}, lists = {} } = {}) => ({
        innerHTML: '',
        querySelector(selector) { return nodes[selector] || null; },
        querySelectorAll(selector) { return lists[selector] || []; },
    });
"""


def _stats_source_for_test() -> str:
    """替换浏览器相邻依赖，并仅为测试暴露内部纯函数和动作。"""

    source = STATS_CLIENT.read_text(encoding="utf-8")
    replacements = (
        ("import { api } from '../api.js';", "const api = globalThis.__api;"),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            """import {
    arrayValue as safeArray,
    errorMessage,
    finiteNumber,
    formatAmount,
    formatMoneyCompact,
    isRecord,
    isValidDateInput,
    noteCadenceSubtitle,
    pad2,
    records as safeRecords,
    todayStr as sharedTodayStr,
} from '../utils/format.js';""",
            r"""const safeArray = (value) => Array.isArray(value) ? value : [];
const isRecord = (value) => value !== null
    && typeof value === 'object'
    && !Array.isArray(value);
const safeRecords = (value) => Array.isArray(value) ? value.filter(isRecord) : [];
const finiteNumber = (value, fallback = 0) => {
    try {
        const normalized = Number(value);
        return Number.isFinite(normalized) ? normalized : fallback;
    } catch {
        return fallback;
    }
};
const errorMessage = (error, fallback = '未知错误') =>
    typeof error?.message === 'string' && error.message.trim() ? error.message.trim() : fallback;
const formatAmount = (value) => `¥${Number(value || 0).toFixed(2)}`;
const formatMoneyCompact = (value) => `${Number(value || 0)}`;
const isValidDateInput = (value) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
};
const pad2 = (value) => String(value).padStart(2, '0');
const noteCadenceSubtitle = (granularity, rangeLabel) => {
    if (granularity === 'year') return `按${rangeLabel}查看每年新增笔记数量。`;
    if (granularity === 'month') return `按${rangeLabel}查看每月新增笔记数量。`;
    if (granularity === 'week') return `按${rangeLabel}查看每周新增笔记数量。`;
    return `按${rangeLabel}查看每天的笔记输入频率。`;
};
const sharedTodayStr = () => '2026-05-20';""",
        ),
        (
            "import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, "
            "todayRangeKey } from '../utils/date_ranges.js';",
            """const RANGE_PRESET_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'quarter', label: '本季度' },
    { key: 'year', label: '今年' },
    { key: 'last_year', label: '去年' },
    { key: 'all', label: '全部' },
    { key: 'custom', label: '自定义' },
];
const todayRangeKey = () => '2026-05-20';
const derivePresetRange = (key, options = {}) => {
    const today = options.today || '2026-05-20';
    if (key === 'custom' && options.customStart && options.customEnd) {
        return { start: options.customStart, end: options.customEnd };
    }
    if (key === 'week') return { start: '2026-05-18', end: today };
    if (key === 'quarter') return { start: '2026-04-01', end: today };
    if (key === 'year') return { start: '2026-01-01', end: today };
    if (key === 'last_year') return { start: '2025-01-01', end: '2025-12-31' };
    if (key === 'all') return { start: '1970-01-01', end: today };
    return { start: '2026-05-01', end: today };
};
const fetchItemRangeBounds = (...args) => globalThis.__fetchItemRangeBounds(...args);""",
        ),
        (
            "import { bindEnterAction, BREAKPOINTS, escapeHtml, injectStyles, mediaMax, "
            "pageShellCss } from '../utils/ui.js';",
            """const bindEnterAction = (element, action) => {
    if (!element || typeof action !== 'function') return;
    element.onkeydown = async (event) => {
        if (event.key === 'Enter' && !event.isComposing) {
            event.preventDefault();
            await action();
        }
    };
};
const BREAKPOINTS = { XL: '1200px', MOBILE: '720px', PHONE: '480px' };
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
    attachListeners as __attachListeners,
    buildAxisTickLabels,
    compressSeries,
    currentRangeRequest,
    diaryCadenceSubtitle,
    diaryCards as __diaryCards,
    diffDays,
    fetchAllData,
    finiteNumber,
    formatCount,
    formatMoodLabel,
    formatPercent,
    formatWordCompact,
    generateInsights,
    loadAndRender as __loadAndRender,
    normalizeTaskTotals,
    normalizeTextMap,
    renderActivityHeatmap,
    renderColumnChart,
    renderDonut,
    renderHeatStrip,
    renderSparkline,
    renderTokenCloud,
    safeArray,
    safeRecords,
    sampleIndexes,
    sumBy,
};
export function __setStatsTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('range' in state) _range = state.range;
    if ('loading' in state) _loading = state.loading;
    if ('data' in state) _data = state.data;
    if ('customStart' in state) _customStart = state.customStart;
    if ('customEnd' in state) _customEnd = state.customEnd;
    if ('customDraftStart' in state) _customDraftStart = state.customDraftStart;
    if ('customDraftEnd' in state) _customDraftEnd = state.customDraftEnd;
    if ('heatmapData' in state) _heatmapData = state.heatmapData;
    if ('comparisonData' in state) _comparisonData = state.comparisonData;
    if ('moodEmojis' in state) _moodEmojis = state.moodEmojis;
    if ('moodLabels' in state) _moodLabels = state.moodLabels;
    if ('loadVersion' in state) _loadVersion = state.loadVersion;
    if ('activeRequestSignature' in state) _activeRequestSignature = state.activeRequestSignature;
    if ('dataSignature' in state) _dataSignature = state.dataSignature;
}
export function __getStatsTestState() {
    return {
        container: _container,
        range: _range,
        loading: _loading,
        data: _data,
        customStart: _customStart,
        customEnd: _customEnd,
        customDraftStart: _customDraftStart,
        customDraftEnd: _customDraftEnd,
        heatmapData: _heatmapData,
        comparisonData: _comparisonData,
        moodEmojis: _moodEmojis,
        moodLabels: _moodLabels,
        loadVersion: _loadVersion,
        activeRequestSignature: _activeRequestSignature,
        dataSignature: _dataSignature,
    };
}
"""
    )


def _run_stats_client(script: str) -> None:
    """在 Node 中执行统计页真实 ESM 数据、图表和生命周期。"""

    assert_node_esm_contract(
        _stats_source_for_test(),
        script,
        cwd=ROOT,
        setup=STATS_SETUP,
    )


def test_stats_page_real_module_imports() -> None:
    """生产模块及其真实依赖图必须可由 ESM 正常解析。"""

    assert_node_esm_contract(
        "export {};",
        "await import('./plugins/pendo/web/static/js/pages/stats.js');",
        cwd=ROOT,
    )


def test_stats_numeric_boundaries_and_sampling_are_finite() -> None:
    """非有限数值、负计数和极端采样参数不得污染统计输出。"""

    _run_stats_client(
        r"""
        assert.equal(client.formatCount(Number.POSITIVE_INFINITY), '0');
        assert.equal(client.formatCount(-5), '0');
        assert.equal(client.formatWordCompact('1200'), '1.2k字');
        assert.equal(client.formatPercent(2), '100%');
        assert.equal(client.formatPercent(-1), '0%');
        assert.equal(client.sumBy({ bad: true }, 'count'), 0);
        assert.equal(client.sumBy([null, { count: '2.5' }, { count: 'bad' }], 'count'), 2.5);

        assert.deepEqual(client.sampleIndexes(20, 6), [0, 4, 8, 11, 15, 19]);
        assert.deepEqual(client.sampleIndexes(20, 1), [0]);
        assert.deepEqual(
            client.buildAxisTickLabels(
                Array.from({ length: 20 }, (_, index) => `2026-05-${String(index + 1).padStart(2, '0')}`),
                6,
            ),
            ['05/01', '05/05', '05/09', '05/12', '05/16', '05/20'],
        );
        assert.deepEqual(client.compressSeries(['a', 'b', 'c'], [1, 9, 3], 2), {
            labels: ['a', 'c'], values: [1, 3],
        });
        assert.equal(client.diffDays('2026-03-07', '2026-03-09'), 2);
        assert.equal(client.diffDays('bad', '2026-03-09'), 0);

        assert.deepEqual(client.normalizeTaskTotals(null), {
            open: 0, done: 0, cancelled: 0, total: 0, completionRate: 0,
        });
        assert.deepEqual(client.normalizeTaskTotals({
            open: '2.4', done: 3.6, cancelled: Infinity, closed: 99,
        }), {
            open: 2, done: 4, cancelled: 0, total: 6, completionRate: 4 / 6,
        });
        """
    )


def test_stats_charts_filter_malformed_rows_and_escape_content() -> None:
    """损坏记录不能令图表崩溃，标签和格式化结果必须安全转义。"""

    _run_stats_client(
        r"""
        const columns = client.renderColumnChart(
            [null, { label: '<script>x</script>', count: Infinity }, { label: '有效', count: 5 }],
            'count', 'label', '#000', (value) => `<${value}>`,
        );
        assert.ok(columns.includes('&lt;script&gt;x&lt;/script&gt;'));
        assert.ok(columns.includes('&lt;5&gt;'));
        assert.ok(!columns.includes('<script>'));
        assert.ok(!columns.includes('Infinity'));
        assert.ok(!columns.includes('NaN'));

        const firstSparkline = client.renderSparkline(['a'], [1], '#10b981', String);
        const secondSparkline = client.renderSparkline(['a'], [1], '#10b981', String);
        const firstGradient = firstSparkline.match(/id="([^"]+)"/)[1];
        const secondGradient = secondSparkline.match(/id="([^"]+)"/)[1];
        assert.notEqual(firstGradient, secondGradient);

        const donut = client.renderDonut(
            [null, { label: '<分类>', count: 4 }, { label: '坏值', count: 'bad' }],
            'count', 'label', [], '4', '总数', (value) => `${value}项`,
        );
        assert.ok(donut.includes('&lt;分类&gt;'));
        assert.ok(donut.includes('<title>'));
        assert.ok(!donut.includes('<分类>'));

        const strip = client.renderHeatStrip(
            [{ label: '<日期>', count: 1 }],
            'count', 'label', '1,2,3', (value) => `<${value}>`, { columns: 999 },
        );
        assert.ok(strip.includes('repeat(31, minmax(0, 1fr))'));
        assert.ok(strip.includes('&lt;日期&gt;'));
        assert.ok(strip.includes('&lt;1&gt;'));

        const cloud = client.renderTokenCloud(
            [null, { label: '<tag>', value: Infinity }],
            (value) => `<${value}>`,
        );
        assert.ok(cloud.includes('&lt;tag&gt;'));
        assert.ok(cloud.includes('&lt;0&gt;'));
        """
    )


def test_stats_activity_heatmap_keeps_zero_cells_neutral() -> None:
    """活动热力图应过滤非法日期，并把零活动与低强度活动明确区分。"""

    _run_stats_client(
        r"""
        const html = client.renderActivityHeatmap([
            null,
            { date: 'bad', count: 99 },
            { date: '2026-01-01', count: 0, ledger: Infinity },
            { date: '2026-01-02', count: 2, note: 2 },
        ], '2026-01-02', '2026-01-02');
        assert.ok(html.includes('background:rgba(226,232,240,0.52)'));
        assert.ok(html.includes('background:rgba(16,185,129,1)'));
        assert.ok(html.includes('stats-heatmap-cell in-range'));
        assert.ok(!html.includes('bad'));
        assert.ok(!html.includes('Infinity'));
        assert.ok(!html.includes('NaN'));
        """
    )


def test_stats_diary_cards_use_word_density_moods_and_period_streak() -> None:
    """日记卡片应按字数和配置情绪渲染，并展示区间最长连续天数。"""

    _run_stats_client(
        r"""
        client.__setStatsTestState({
            range: 'year',
            moodEmojis: { happy: '🙂' },
            moodLabels: { happy: '开心' },
            data: { diary: {
                summary: {
                    entry_count: 2,
                    current_streak: 3,
                    fill_rate: 0.5,
                    period_longest_streak: 12,
                    total_words: 2400,
                },
                cadence_granularity: 'day',
                cadence: [{ label: '2026-05-20', words: 1200, mood: 'happy' }],
                mood_breakdown: [{ mood: 'happy', count: 2 }],
                template_usage: [{ template_id: '<模板>', count: 2 }],
            } },
        });
        assert.equal(client.diaryCadenceSubtitle('year'), '今年里每年写了多少字。');
        assert.equal(client.formatMoodLabel('happy'), '🙂 开心');
        const html = client.__diaryCards();
        assert.ok(html.includes('1.2k字'));
        assert.ok(html.includes('🙂 开心'));
        assert.ok(html.includes('区间最长连续'));
        assert.ok(html.includes('>12<'));
        assert.ok(html.includes('&lt;模板&gt;'));
        assert.ok(!html.includes('<模板>'));
        """
    )


def test_stats_fetches_regular_range_with_declared_params_and_safe_moods() -> None:
    """普通范围应复用统一参数，并拒绝数组冒充配置或统计对象。"""

    _run_stats_client(
        r"""
        __api.get = async (path, params = {}) => {
            __apiCalls.push({ path, params });
            if (path === '/stats/ledger') return { data: [] };
            if (path === '/config/diary/moods') {
                return { data: { mood_emojis: [], mood_labels: { happy: ' 开心 ' } } };
            }
            return { data: { marker: path } };
        };
        const result = await client.fetchAllData({
            rangeKey: 'month',
            range: { start: '2026-05-01', end: '2026-05-20' },
            today: '2026-05-20',
        });
        assert.equal(__boundsCalls.length, 0);
        assert.deepEqual(result.data.ledger, {});
        assert.equal(result.data.tasks.marker, '/stats/tasks');
        assert.equal(result.moodEmojis.happy, '😊');
        assert.equal(result.moodLabels.happy, '开心');

        const byPath = Object.fromEntries(__apiCalls.map((call) => [call.path, call.params]));
        assert.deepEqual(byPath['/stats/ledger'], { range: '2026-05-01..2026-05-20' });
        assert.deepEqual(byPath['/stats/tasks'], { range: '2026-05-01..2026-05-20' });
        assert.deepEqual(byPath['/stats/events'], { range: '2026-05-01..2026-05-20' });
        assert.deepEqual(byPath['/stats/notes/overview'], {
            start_date: '2026-05-01', end_date: '2026-05-20', today: '2026-05-20',
        });
        assert.deepEqual(byPath['/stats/diary/overview'], {
            start_date: '2026-05-01', end_date: '2026-05-20',
            today: '2026-05-20', cadence_granularity: 'auto',
        });
        assert.deepEqual(byPath['/stats/activity-heatmap'], { year: 2026 });
        assert.deepEqual(byPath['/stats/ledger/comparison'], { months: 6 });
        """
    )


def test_stats_all_range_resolves_three_boundaries_in_parallel() -> None:
    """全部时间的账本、笔记和日记边界必须并行解析后分别透传。"""

    _run_stats_client(
        r"""
        const pendingBounds = [];
        __fetchItemRangeBounds = async (_api, options) => {
            const pending = __deferred();
            pendingBounds.push({ options, ...pending });
            return pending.promise;
        };
        __api.get = async (path, params = {}) => {
            __apiCalls.push({ path, params });
            return { data: {} };
        };
        const request = client.fetchAllData({
            rangeKey: 'all',
            range: { start: '1970-01-01', end: '2026-05-20' },
            today: '2026-05-20',
        });
        await __flushPromises();
        assert.equal(pendingBounds.length, 3);
        assert.deepEqual(pendingBounds.map((item) => item.options.type), ['ledger', 'note', 'diary']);
        assert.equal(__apiCalls.length, 0);

        pendingBounds[0].resolve({ start: '2020-01-01', end: '2026-05-20' });
        pendingBounds[1].resolve({ start: '2021-02-01', end: '2026-05-19' });
        pendingBounds[2].resolve({ start: '2022-03-01', end: '2026-05-18' });
        await request;
        const byPath = Object.fromEntries(__apiCalls.map((call) => [call.path, call.params]));
        assert.deepEqual(byPath['/stats/ledger'], {
            start_date: '2020-01-01', end_date: '2026-05-20',
        });
        assert.deepEqual(byPath['/stats/notes/overview'], {
            start_date: '2021-02-01', end_date: '2026-05-19', today: '2026-05-19',
        });
        assert.deepEqual(byPath['/stats/diary/overview'], {
            start_date: '2022-03-01', end_date: '2026-05-18',
            today: '2026-05-18', cadence_granularity: 'auto',
        });
        """
    )


def test_stats_latest_load_wins_deduplicates_and_destroy_ignores_late_data() -> None:
    """相同在途请求只发一次，后发范围胜出，销毁后迟到响应不得回写。"""

    _run_stats_client(
        r"""
        const batches = [__deferred(), __deferred(), __deferred(), __deferred()];
        let apiCallCount = 0;
        __api.get = (path) => {
            const batchIndex = Math.floor(apiCallCount / 8);
            apiCallCount += 1;
            return batches[batchIndex].promise.then((marker) => {
                if (path === '/stats/ledger') return { data: { marker } };
                if (path === '/stats/activity-heatmap') return { data: { year: 2026, days: [] } };
                if (path === '/stats/ledger/comparison') return { data: { months: [] } };
                return { data: {} };
            });
        };

        const root = __makeRoot();
        const oldLoad = client.render(root);
        await __flushPromises();
        client.__setStatsTestState({ range: 'week' });
        const latestLoad = client.__loadAndRender();
        await __flushPromises();
        assert.equal(apiCallCount, 16);

        batches[1].resolve('最新');
        await latestLoad;
        assert.equal(client.__getStatsTestState().data.ledger.marker, '最新');
        batches[0].resolve('旧值');
        await oldLoad;
        assert.equal(client.__getStatsTestState().data.ledger.marker, '最新');

        client.__setStatsTestState({ range: 'quarter' });
        const firstSame = client.__loadAndRender();
        const duplicate = client.__loadAndRender();
        await __flushPromises();
        assert.equal(apiCallCount, 24);
        batches[2].resolve('季度');
        await Promise.all([firstSame, duplicate]);
        assert.equal(client.__getStatsTestState().data.ledger.marker, '季度');

        client.__setStatsTestState({ range: 'year' });
        const lateLoad = client.__loadAndRender();
        await __flushPromises();
        assert.equal(apiCallCount, 32);
        const htmlBeforeDestroy = root.innerHTML;
        client.destroy();
        batches[3].resolve('迟到');
        await lateLoad;
        const state = client.__getStatsTestState();
        assert.equal(state.container, null);
        assert.equal(state.data, null);
        assert.equal(root.innerHTML, htmlBeforeDestroy);
        assert.deepEqual(__toastCalls, []);
        """
    )


def test_stats_custom_range_is_validated_ime_safe_and_not_resubmitted() -> None:
    """自定义范围应保留草稿、忽略输入法 Enter，并避免重复提交同一日期。"""

    _run_stats_client(
        r"""
        const start = __makeControl({ value: 'bad' });
        const end = __makeControl({ value: '2026-05-20' });
        const apply = __makeControl();
        const invalidRangeButton = __makeControl({ dataset: { range: 'hostile' } });
        const root = __makeRoot({
            nodes: {
                '#stats-custom-start': start,
                '#stats-custom-end': end,
                '#stats-custom-apply': apply,
            },
            lists: { '.stats-range-btn[data-range]': [invalidRangeButton] },
        });
        client.__setStatsTestState({
            container: root,
            range: 'custom',
            customStart: '2026-05-01',
            customEnd: '2026-05-20',
            data: {},
            dataSignature: 'custom|2026-05-01|2026-05-20',
        });
        client.__attachListeners();
        await invalidRangeButton.onclick();
        assert.equal(client.__getStatsTestState().range, 'custom');

        let prevented = 0;
        await start.onkeydown({
            key: 'Enter', isComposing: true, preventDefault() { prevented += 1; },
        });
        assert.equal(prevented, 0);
        await apply.onclick();
        assert.deepEqual(__toastCalls.at(-1), ['请输入有效日期，格式为 YYYY-MM-DD', 'error']);

        start.value = '2026-05-21';
        end.value = '2026-05-20';
        await apply.onclick();
        assert.deepEqual(__toastCalls.at(-1), ['开始日期不能晚于结束日期', 'error']);

        __apiCalls.length = 0;
        start.value = '2026-04-01';
        end.value = '2026-05-20';
        await apply.onclick();
        assert.equal(__apiCalls.length, 8);
        assert.equal(client.__getStatsTestState().customStart, '2026-04-01');
        assert.equal(client.__getStatsTestState().customEnd, '2026-05-20');
        await apply.onclick();
        assert.equal(__apiCalls.length, 8);
        """
    )


def test_stats_render_is_safe_accessible_and_load_failure_recovers() -> None:
    """整页渲染应转义接口文本、保留焦点样式，并用稳定错误消息收口。"""

    _run_stats_client(
        r"""
        __api.get = async (path) => {
            if (path === '/stats/ledger') {
                return { data: { expense_by_category: [{ category: '<script>分类</script>', total: 2 }] } };
            }
            if (path === '/stats/activity-heatmap') return { data: { year: 2026, days: [] } };
            if (path === '/stats/ledger/comparison') return { data: { months: [] } };
            return { data: {} };
        };
        const root = __makeRoot();
        await client.render(root);
        assert.ok(root.innerHTML.includes('&lt;script&gt;分类&lt;/script&gt;'));
        assert.ok(!root.innerHTML.includes('<script>分类</script>'));
        assert.ok(root.innerHTML.includes('type="button"'));
        assert.ok(root.innerHTML.includes('aria-pressed="true"'));
        assert.ok(__styleCalls.at(-1)[1].includes('.stats-range-btn:focus-visible'));
        assert.ok(__styleCalls.at(-1)[1].includes('font-size: clamp(24px, 1.85vw, 30px)'));
        await assert.rejects(() => client.render(null), /有效的容器元素/);

        __api.get = async () => { throw {}; };
        const failedRoot = __makeRoot();
        await client.render(failedRoot);
        assert.deepEqual(__toastCalls.at(-1), ['加载统计失败：未知错误', 'error']);
        assert.equal(client.__getStatsTestState().loading, false);
        assert.ok(!failedRoot.innerHTML.includes('正在加载统计视图'));
        """
    )
