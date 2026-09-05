"""Pendo Web hash 路由的解析、订阅和异步页面生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final          = REPOSITORY_ROOT
ROUTER_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "router.js"

ROUTER_SETUP: Final = r"""
    globalThis.__state = {
        errors: [],
        listenerCounts: {},
    };
    globalThis.window = {
        location: { hash: '' },
        listeners: {},
        addEventListener(type, callback) {
            __state.listenerCounts[type] = (__state.listenerCounts[type] || 0) + 1;
            this.listeners[type] = callback;
        },
    };
    globalThis.console.error = (...args) => {
        __state.errors.push(args.map((value) => String(value)).join(' '));
    };
    globalThis.__escapeHtml = (value) => String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    globalThis.__timeZoneLoads = 0;
    globalThis.__fetchUserTimeZone = async () => {
        __timeZoneLoads += 1;
        return 'Asia/Shanghai';
    };
"""


def _router_source_for_test() -> str:
    """仅替换 UI 转义依赖，保留真实路由实现。"""

    source = ROUTER_CLIENT.read_text(encoding="utf-8")
    ui_import       = "import { escapeHtml } from './utils/ui.js';"
    timezone_import = "import { fetchUserTimeZone } from './utils/timezone.js';"
    assert ui_import in source
    assert timezone_import in source
    return source.replace(
        ui_import,
        "const escapeHtml = globalThis.__escapeHtml;",
    ).replace(
        timezone_import,
        "const fetchUserTimeZone = globalThis.__fetchUserTimeZone;",
    )


def _run_router_client(script: str) -> None:
    """在最小浏览器桩上执行真实路由模块。"""

    assert_node_esm_contract(
        _router_source_for_test(),
        script,
        cwd   = ROOT,
        setup = ROUTER_SETUP,
    )


def test_router_parses_hashes_and_builds_navigation_targets() -> None:
    """空路由、标准路由和无斜线 hash 都应稳定解析查询参数。"""

    _run_router_client(
        r"""
        assert.equal(client.getParams().path, 'dashboard');

        window.location.hash = '#/tasks?q=a+b&tag=x';
        let route = client.getParams();
        assert.equal(route.path, 'tasks');
        assert.equal(route.params.get('q'), 'a b');
        assert.equal(route.params.get('tag'), 'x');

        window.location.hash = '#tasks?x=1';
        route = client.getParams();
        assert.equal(route.path, 'tasks');
        assert.equal(route.params.get('x'), '1');

        window.location.hash = '#/?from=empty';
        route = client.getParams();
        assert.equal(route.path, 'dashboard');
        assert.equal(route.params.get('from'), 'empty');

        client.navigate('search?q=%E6%B5%8B%E8%AF%95');
        assert.equal(window.location.hash, '#/search?q=%E6%B5%8B%E8%AF%95');
        """
    )


def test_router_uses_one_hash_listener_and_isolates_subscriber_failures() -> None:
    """多个订阅者共享一个监听器，单个回调异常不能阻断页面导航。"""

    _run_router_client(
        r"""
        const paths = [];
        window.location.hash = '#/dashboard';

        client.onRouteChange(() => { throw new Error('subscriber failed'); });
        const unsubscribe = client.onRouteChange((path) => paths.push(path));
        assert.deepEqual(paths, ['dashboard']);
        assert.equal(__state.listenerCounts.hashchange, 1);
        assert.equal(__state.errors.length, 1);

        client.registerRoute('dashboard', async () => ({
            render(container) { container.innerHTML = 'dashboard'; },
        }));
        client.registerRoute('tasks', async () => ({
            render(container) { container.innerHTML = 'tasks'; },
        }));

        const firstContainer = { innerHTML: '' };
        await client.init(firstContainer);
        assert.equal(firstContainer.innerHTML, 'dashboard');
        assert.equal(__state.listenerCounts.hashchange, 1);

        window.location.hash = '#/tasks';
        await window.listeners.hashchange();
        assert.equal(firstContainer.innerHTML, 'tasks');
        assert.deepEqual(paths, ['dashboard', 'tasks']);
        assert.equal(__state.errors.length, 2);

        unsubscribe();
        window.location.hash = '#/dashboard';
        await window.listeners.hashchange();
        assert.deepEqual(paths, ['dashboard', 'tasks']);

        const secondContainer = { innerHTML: '' };
        await client.init(secondContainer);
        assert.equal(secondContainer.innerHTML, 'dashboard');
        assert.equal(__state.listenerCounts.hashchange, 1);
        assert.equal(__timeZoneLoads, 4);
        """
    )


def test_router_serializes_async_pages_and_only_loads_the_latest_route() -> None:
    """快速连续跳转时旧页只清理一次，中间页不装载，最终页独占内容区。"""

    _run_router_client(
        r"""
        const calls = { slowDestroy: 0, middleLoad: 0, fastLoad: 0 };
        const order = [];
        let releaseSlow;
        let markSlowStarted;
        const slowStarted = new Promise((resolve) => { markSlowStarted = resolve; });
        const slowRelease = new Promise((resolve) => { releaseSlow = resolve; });

        client.registerRoute('slow', async () => ({
            async render(container) {
                container.innerHTML = 'slow-started';
                markSlowStarted();
                await slowRelease;
                container.innerHTML = 'slow-finished';
            },
            destroy() { calls.slowDestroy += 1; },
        }));
        client.registerRoute('middle', async () => {
            calls.middleLoad += 1;
            return { render(container) { container.innerHTML = 'middle'; } };
        });
        client.registerRoute('fast', async () => {
            calls.fastLoad += 1;
            return {
                async onRouteEnter(params) {
                    order.push(`enter:${params.get('source')}`);
                    await Promise.resolve();
                    order.push('entered');
                },
                render(container) {
                    order.push('render');
                    container.innerHTML = 'fast';
                },
            };
        });

        const container = { innerHTML: '' };
        window.location.hash = '#/slow';
        const initialNavigation = client.init(container);
        await slowStarted;

        window.location.hash = '#/middle';
        const middleNavigation = window.listeners.hashchange();
        assert.equal(calls.slowDestroy, 1);
        assert.match(container.innerHTML, /加载中/);

        window.location.hash = '#/fast?source=latest';
        const fastNavigation = window.listeners.hashchange();
        releaseSlow();
        await Promise.all([initialNavigation, middleNavigation, fastNavigation]);

        assert.equal(calls.slowDestroy, 1);
        assert.equal(calls.middleLoad, 0);
        assert.equal(calls.fastLoad, 1);
        assert.deepEqual(order, ['enter:latest', 'entered', 'render']);
        assert.equal(container.innerHTML, 'fast');
        """
    )


def test_router_escapes_errors_cleans_pages_and_recovers_a_rejected_queue() -> None:
    """未知页与失败页应安全呈现；一次意外队列拒绝后仍可继续导航。"""

    _run_router_client(
        r"""
        window.location.hash = '#/missing';
        const initialContainer = { innerHTML: '' };
        await client.init(initialContainer);
        assert.match(initialContainer.innerHTML, /页面不存在/);

        let destroyCount = 0;
        client.registerRoute('broken', async () => ({
            render() { throw new Error('<bad>'); },
            destroy() { destroyCount += 1; },
        }));

        let brittleHtml = '';
        let rejectErrorMarkup = true;
        const brittleContainer = {
            get innerHTML() { return brittleHtml; },
            set innerHTML(value) {
                if (rejectErrorMarkup && value.includes('error-state')) {
                    rejectErrorMarkup = false;
                    throw new Error('container failed');
                }
                brittleHtml = value;
            },
        };
        window.location.hash = '#/broken';
        await assert.rejects(client.init(brittleContainer), /container failed/);
        assert.equal(destroyCount, 1);

        const recoveredContainer = { innerHTML: '' };
        await client.init(recoveredContainer);
        assert.equal(destroyCount, 2);
        assert.match(recoveredContainer.innerHTML, /加载失败：&lt;bad&gt;/);
        assert.ok(!recoveredContainer.innerHTML.includes('加载失败：<bad>'));

        client.registerRoute('noisy', async () => ({
            render(container) { container.innerHTML = 'noisy'; },
            destroy() { throw new Error('destroy failed'); },
        }));
        client.registerRoute('safe', async () => ({
            render(container) { container.innerHTML = 'safe'; },
        }));
        window.location.hash = '#/noisy';
        await window.listeners.hashchange();
        window.location.hash = '#/safe';
        await window.listeners.hashchange();

        assert.equal(recoveredContainer.innerHTML, 'safe');
        assert.ok(__state.errors.some((message) => message.includes('页面加载失败')));
        assert.ok(__state.errors.some((message) => message.includes('页面清理失败')));
        """
    )
