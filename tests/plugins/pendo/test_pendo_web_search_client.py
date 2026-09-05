"""Pendo Web 搜索页的数据边界、交互与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_timezone_test_support import inline_timezone_runtime

ROOT: Final = REPOSITORY_ROOT
SEARCH_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "search.js"
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)

SEARCH_SETUP: Final = r"""
    globalThis.__api = { get: async () => ({ data: {} }) };
    globalThis.__toastCalls = [];
    globalThis.__paginationCalls = [];
    globalThis.__styleCalls = [];
    globalThis.__unsubscribeCount = 0;
    globalThis.__dataChangeCallback = null;
    globalThis.__eventDetailCalls = [];
    globalThis.__taskModalCalls = [];
    globalThis.__ledgerModalCalls = [];
    globalThis.__noteModalCalls = [];
    globalThis.__diaryModalCalls = [];
    globalThis.__subscribeDataChanges = (_type, callback) => {
        __dataChangeCallback = callback;
        return () => { __unsubscribeCount += 1; };
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
        onchange: null,
        onkeydown: null,
        focusCount: 0,
        selection: null,
        focus() { this.focusCount += 1; },
        setSelectionRange(start, end) { this.selection = [start, end]; },
        ...extra,
    });
    globalThis.__makeRoot = ({ nodes = {}, lists = {} } = {}) => ({
        innerHTML: '',
        querySelector(selector) { return nodes[selector] || null; },
        querySelectorAll(selector) { return lists[selector] || []; },
    });
"""


def _search_source_for_test() -> str:
    """替换浏览器相邻依赖，并嵌入真实共享格式函数。"""

    source = SEARCH_CLIENT.read_text(encoding="utf-8")
    timezone_runtime = inline_timezone_runtime(TIMEZONE_CLIENT)
    format_source = FORMAT_CLIENT.read_text(encoding="utf-8").replace("export ", "")
    format_runtime = f"""
const {{
    errorMessage,
    finiteNumber,
    isRecord,
    nonNegativeInteger,
    previewText,
    textValue,
}} = (() => {{
{format_source}
    return {{
        errorMessage,
        finiteNumber,
        isRecord,
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
            "import { renderPagination } from '../components/pagination.js';",
            "const renderPagination = (...args) => globalThis.__paginationCalls.push(args);",
        ),
        (
            """import {
    errorMessage,
    finiteNumber,
    isRecord,
    nonNegativeInteger,
    previewText,
    textValue,
} from '../utils/format.js';""",
            format_runtime,
        ),
        (
            "import { formatZonedDateTime } from '../utils/timezone.js';",
            timezone_runtime,
        ),
        (
            "import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, "
            "subscribeDataChanges } from '../utils/ui.js';",
            """const BREAKPOINTS = { MOBILE: '720px' };
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
        (
            "import { openEventDetail } from './events.js';",
            "const openEventDetail = async (...args) => globalThis.__eventDetailCalls.push(args);",
        ),
        (
            "import { openTaskModal } from './tasks.js';",
            "const openTaskModal = (...args) => globalThis.__taskModalCalls.push(args);",
        ),
        (
            "import { openDetailModal as openLedgerDetailModal } from './ledger.js';",
            "const openLedgerDetailModal = (...args) => globalThis.__ledgerModalCalls.push(args);",
        ),
        (
            "import { openNoteViewModal } from './notes.js';",
            "const openNoteViewModal = (...args) => globalThis.__noteModalCalls.push(args);",
        ),
        (
            "import { openDiaryViewModal } from './diary.js';",
            "const openDiaryViewModal = (...args) => globalThis.__diaryModalCalls.push(args);",
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
    doSearch as __doSearch,
    itemMeta,
    itemPreview,
    itemTitle,
    normalizeSearchItem,
    normalizeSearchResponse,
    openResultDetail as __openResultDetail,
    renderCard,
    renderCategoryChips,
    renderSummary,
    searchParams,
};
export function __setSearchTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('query' in state) _query = state.query;
    if ('activeType' in state) _activeType = state.activeType;
    if ('activeCategory' in state) _activeCategory = state.activeCategory;
    if ('activeCategoryField' in state) _activeCategoryField = state.activeCategoryField;
    if ('activeCategoryTypeHint' in state) _activeCategoryTypeHint = state.activeCategoryTypeHint;
    if ('results' in state) _results = state.results;
    if ('total' in state) _total = state.total;
    if ('page' in state) _page = state.page;
    if ('loading' in state) _loading = state.loading;
    if ('hasSearched' in state) _hasSearched = state.hasSearched;
    if (state.resetSignature !== false) _lastSearchSignature = '';
}
export function __getSearchTestState() {
    return {
        container: _container,
        query: _query,
        activeType: _activeType,
        activeCategory: _activeCategory,
        activeCategoryField: _activeCategoryField,
        activeCategoryTypeHint: _activeCategoryTypeHint,
        results: _results,
        total: _total,
        page: _page,
        loading: _loading,
        hasSearched: _hasSearched,
    };
}
"""
    )


def _run_search_client(script: str) -> None:
    """在 Node 中执行搜索页真实 ESM 数据、渲染、详情和生命周期。"""

    assert_node_esm_contract(
        _search_source_for_test(),
        script,
        cwd   = ROOT,
        setup = SEARCH_SETUP,
    )


def test_search_page_real_module_imports_all_detail_contracts() -> None:
    """真实模块图必须能解析所有详情导出，尤其是账本详情入口。"""

    assert_node_esm_contract(
        "export {};",
        "await import('./plugins/pendo/web/static/js/pages/search.js');",
        cwd=ROOT,
    )


def test_search_normalizes_boundaries_and_renders_safe_native_cards() -> None:
    """未知结果与非有限数值不得进入页面，卡片应安全且可键盘操作。"""

    _run_search_client(
        r"""
        assert.equal(client.normalizeSearchItem(null), null);
        assert.equal(client.normalizeSearchItem({ id: 'x', type: '__proto__' }), null);
        assert.equal(client.normalizeSearchItem({ id: '', type: 'note' }), null);

        const item = client.normalizeSearchItem({
            id: 'ledger/a b',
            type: 'ledger',
            title: '<script>账目</script>',
            remark: '<img src=x>',
            transaction_type: 'hostile',
            amount: Number.POSITIVE_INFINITY,
            ledger_category: '<分类>',
            account_name: '<账户>',
        });
        assert.equal(item.transaction_type, 'expense');
        assert.equal(item.amount, null);
        assert.ok(!client.itemMeta(item).join(' ').includes('Infinity'));
        assert.ok(!client.itemMeta(item).join(' ').includes('NaN'));

        const task = client.normalizeSearchItem({
            id: 'task/1', type: 'task', plan_date: '2026-05-20',
        });
        assert.ok(client.itemMeta(task).includes('计划 2026-05-20'));
        assert.ok(!client.itemMeta(task).join(' ').includes('00:00'));

        const zonedEvent = client.normalizeSearchItem({
            id: 'event/zoned', type: 'event', start_time: '2026-05-01T16:30:00+00:00',
        });
        assert.ok(client.itemMeta(zonedEvent).includes('2026-05-02 00:30'));

        const event = client.normalizeSearchItem({
            id: 'event/1',
            type: 'event',
            title: '<标题>',
            notes: '<正文>',
            collection: { title: '<集合>', location: '<地点>' },
        });
        assert.equal(client.itemTitle(event), '<集合> · <标题>');
        const html = client.renderCard(event);
        assert.ok(html.includes('<button class="search-card search-type-event"'));
        assert.ok(html.includes('type="button"'));
        assert.ok(html.includes('data-open-result="event/1"'));
        assert.ok(html.includes('&lt;集合&gt; · &lt;标题&gt;'));
        assert.ok(!html.includes('style="'));
        assert.ok(!html.includes('data-id='));
        assert.ok(!html.includes('<标题>'));

        const response = client.normalizeSearchResponse({
            items: [event, null, { id: 'bad', type: 'unknown' }],
            total: Number.NaN,
        });
        assert.equal(response.items.length, 1);
        assert.equal(response.total, 1);
        """
    )


def test_search_uses_dedicated_category_params_and_normalizes_response() -> None:
    """普通分类与账目分类必须映射到正确参数，分页响应需收敛。"""

    _run_search_client(
        r"""
        const root = __makeRoot();
        client.__setSearchTestState({
            container: root,
            query: ' 餐饮 ',
            activeCategory: '餐饮',
            activeCategoryField: 'ledger_category',
            activeCategoryTypeHint: 'ledger',
            page: 2,
        });
        const calls = [];
        __api.get = async (path, params) => {
            calls.push({ path, params });
            return {
                data: {
                    total: '21',
                    items: [
                        { id: 'l1', type: 'ledger', title: '午餐', amount: '12.5' },
                        null,
                        { id: 'bad', type: 'unknown' },
                    ],
                },
            };
        };
        await client.__doSearch();
        assert.deepEqual(calls[0], {
            path: '/search',
            params: {
                q: '餐饮',
                page: 2,
                page_size: 20,
                ledger_category: '餐饮',
                type: 'ledger',
            },
        });
        const state = client.__getSearchTestState();
        assert.equal(state.results.length, 1);
        assert.equal(state.results[0].amount, 12.5);
        assert.equal(state.total, 21);
        assert.equal(state.hasSearched, true);
        assert.ok(root.innerHTML.includes('午餐'));

        assert.deepEqual(client.searchParams('笔记', 1), {
            q: '笔记',
            page: 1,
            page_size: 20,
            ledger_category: '餐饮',
            type: 'ledger',
        });
        client.__setSearchTestState({
            activeType: 'note',
            activeCategory: '学习',
            activeCategoryField: 'category',
            activeCategoryTypeHint: '',
        });
        assert.deepEqual(client.searchParams('阅读', 1), {
            q: '阅读', page: 1, page_size: 20, type: 'note', category: '学习',
        });
        """
    )


def test_search_deduplicates_same_request_and_latest_query_wins() -> None:
    """同一签名的重复提交只请求一次，快速输入时后发结果胜出。"""

    _run_search_client(
        r"""
        const root = __makeRoot();
        client.__setSearchTestState({ container: root, query: '同一个查询' });
        const pending = [];
        __api.get = (path, params) => {
            const deferred = __deferred();
            pending.push({ path, params, ...deferred });
            return deferred.promise;
        };
        const first = client.__doSearch();
        const duplicate = client.__doSearch();
        await __flushPromises();
        assert.equal(pending.length, 1);
        pending[0].resolve({ data: { total: 1, items: [{ id: 'same', type: 'note', title: '同一结果' }] } });
        await Promise.all([first, duplicate]);

        client.__setSearchTestState({ query: '旧查询' });
        const oldSearch = client.__doSearch();
        await __flushPromises();
        client.__setSearchTestState({ query: '新查询' });
        const newSearch = client.__doSearch();
        await __flushPromises();
        assert.equal(pending.length, 3);

        pending[2].resolve({ data: { total: 1, items: [{ id: 'new', type: 'note', title: '新结果' }] } });
        await newSearch;
        const latestHtml = root.innerHTML;
        assert.ok(latestHtml.includes('新结果'));

        pending[1].resolve({ data: { total: 1, items: [{ id: 'old', type: 'note', title: '旧结果' }] } });
        await oldSearch;
        assert.equal(root.innerHTML, latestHtml);
        assert.ok(!root.innerHTML.includes('旧结果'));
        """
    )


def test_search_clamps_page_and_destroy_ignores_late_response() -> None:
    """结果减少时回到有效页，销毁后迟到响应不能覆盖 DOM。"""

    _run_search_client(
        r"""
        const root = __makeRoot();
        const pages = [];
        __api.get = async (_path, params) => {
            pages.push(params.page);
            return params.page === 3
                ? { data: { total: 1, items: [] } }
                : { data: { total: 1, items: [{ id: 'only', type: 'task', title: '唯一结果' }] } };
        };
        client.__setSearchTestState({ container: root, query: '任务', page: 3 });
        await client.__doSearch();
        assert.deepEqual(pages, [3, 1]);
        assert.equal(client.__getSearchTestState().page, 1);
        assert.ok(root.innerHTML.includes('唯一结果'));

        const late = __deferred();
        __api.get = () => late.promise;
        client.__setSearchTestState({ query: '迟到查询' });
        const request = client.__doSearch();
        await __flushPromises();
        const htmlBeforeDestroy = root.innerHTML;
        client.destroy();
        late.resolve({ data: { total: 1, items: [{ id: 'late', type: 'note', title: '迟到结果' }] } });
        await request;
        assert.equal(root.innerHTML, htmlBeforeDestroy);
        assert.ok(!root.innerHTML.includes('迟到结果'));
        """
    )


def test_search_route_mounts_before_request_and_input_is_ime_safe() -> None:
    """路由只保存查询，挂载后再请求；输入法 Enter 与失焦不得重复搜索。"""

    _run_search_client(
        r"""
        let getCalls = 0;
        __api.get = async () => {
            getCalls += 1;
            return { data: { total: 0, items: [] } };
        };
        client.onRouteEnter(new URLSearchParams('q=%20%E4%BC%9A%E8%AE%AE%20'));
        assert.equal(getCalls, 0);
        const mountedRoot = __makeRoot();
        await client.render(mountedRoot);
        assert.equal(getCalls, 1);
        assert.equal(client.__getSearchTestState().query, '会议');
        assert.equal(typeof __dataChangeCallback, 'function');

        const input = __makeControl({ value: '新会议' });
        const root = __makeRoot({ nodes: { '#search-input': input } });
        client.__setSearchTestState({
            container: root,
            query: '',
            results: [],
            total: 0,
            page: 1,
            hasSearched: false,
        });
        client.__attachListeners();
        await input.onkeydown({
            key: 'Enter', isComposing: true, preventDefault() { throw new Error('不应提交'); },
        });
        assert.equal(getCalls, 1);

        await input.onkeydown({ key: 'Enter', isComposing: false, preventDefault() {} });
        assert.equal(getCalls, 2);
        await input.onchange();
        assert.equal(getCalls, 2);

        await input.onkeydown({ key: 'Escape', isComposing: false, preventDefault() {} });
        assert.equal(client.__getSearchTestState().query, '');
        assert.equal(client.__getSearchTestState().hasSearched, false);

        client.onRouteEnter(new URLSearchParams());
        const secondRoot = __makeRoot();
        await client.render(secondRoot);
        assert.equal(__unsubscribeCount, 1);
        client.destroy();
        assert.equal(__unsubscribeCount, 2);
        await assert.rejects(() => client.render(null), /有效的容器元素/);
        """
    )


def test_search_detail_uses_encoded_id_and_routes_normalized_type() -> None:
    """详情查询必须编码 ID，并只分发搜索结果声明的受支持类型。"""

    _run_search_client(
        r"""
        await client.__openResultDetail({ id: 'event/a b', type: 'event' });
        assert.deepEqual(__eventDetailCalls, [['event/a b']]);

        const getCalls = [];
        __api.get = async (...args) => {
            getCalls.push(args);
            return { data: { id: 'wrong', type: 'diary', title: '<最新标题>' } };
        };
        await client.__openResultDetail({ id: 'task/a b?', type: 'task', title: '旧标题' });
        assert.deepEqual(getCalls, [['/items/task%2Fa%20b%3F']]);
        assert.equal(__taskModalCalls.length, 1);
        assert.equal(__taskModalCalls[0][0].id, 'task/a b?');
        assert.equal(__taskModalCalls[0][0].type, 'task');
        assert.equal(__taskModalCalls[0][0].title, '<最新标题>');
        assert.equal(__diaryModalCalls.length, 0);

        await client.__openResultDetail({ id: 'ledger/1', type: 'ledger' });
        await client.__openResultDetail({ id: 'note/1', type: 'note' });
        await client.__openResultDetail({ id: 'diary/1', type: 'diary' });
        assert.equal(__ledgerModalCalls.length, 1);
        assert.equal(__noteModalCalls.length, 1);
        assert.equal(__diaryModalCalls.length, 1);

        const callCount = getCalls.length;
        await client.__openResultDetail({ id: 'bad', type: 'unknown' });
        assert.equal(getCalls.length, callCount);
        """
    )
