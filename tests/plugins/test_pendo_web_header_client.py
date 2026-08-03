"""Pendo Web 顶栏的路由、搜索和退出交互回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
HEADER_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "header.js"
)

HEADER_SETUP: Final = r"""
    globalThis.__state = {
        navigations: [],
        routeCallbacks: [],
        documentEvents: [],
        logoutCalls: 0,
        reloads: 0,
        errors: [],
        logoutImpl: async () => {},
    };
    globalThis.console.error = (...args) => {
        __state.errors.push(args.map((value) => String(value)).join(' '));
    };
    globalThis.__api = {
        logout: () => {
            __state.logoutCalls += 1;
            return __state.logoutImpl();
        },
    };
    globalThis.__router = {
        navigate: (path) => __state.navigations.push(path),
        onRouteChange: (callback) => {
            __state.routeCallbacks.push(callback);
            callback('dashboard');
            return () => {};
        },
    };

    class FakeElement {
        constructor(tagName = 'div') {
            this.tagName = tagName;
            this.className = '';
            this.textContent = '';
            this.value = '';
            this.disabled = false;
            this.listeners = new Map();
            this.nodes = new Map();
            this.children = [];
            this._innerHTML = '';
        }
        set innerHTML(value) {
            this._innerHTML = String(value);
            for (const selector of [
                '.header-title',
                '.header-toggle',
                '.header-search-input',
                '.header-search-toggle',
                '.header-logout',
            ]) {
                this.nodes.set(selector, new FakeElement());
            }
        }
        get innerHTML() { return this._innerHTML; }
        appendChild(child) { this.children.push(child); return child; }
        querySelector(selector) { return this.nodes.get(selector) || null; }
        addEventListener(type, callback) {
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(callback);
        }
        emit(type, init = {}) {
            const event = { key: init.key, isComposing: Boolean(init.isComposing) };
            return (this.listeners.get(type) || []).map((callback) => callback(event));
        }
    }

    globalThis.document = {
        createElement: (tagName) => new FakeElement(tagName),
        dispatchEvent: (event) => __state.documentEvents.push(event.type),
    };
    globalThis.CustomEvent = class CustomEvent {
        constructor(type) { this.type = type; }
    };
    globalThis.window = {
        location: { reload: () => { __state.reloads += 1; } },
    };
    globalThis.__FakeElement = FakeElement;
"""


def _header_source_for_test() -> str:
    """只替换 API 与路由依赖，保留顶栏模块的真实实现。"""

    source = HEADER_CLIENT.read_text(encoding="utf-8")
    api_import = "import { logout } from '../api.js';"
    router_import = "import { navigate, onRouteChange } from '../router.js';"
    assert api_import in source
    assert router_import in source
    return source.replace(api_import, "const { logout } = globalThis.__api;").replace(
        router_import,
        "const { navigate, onRouteChange } = globalThis.__router;",
    )


def _run_header_client(script: str) -> None:
    """在最小 DOM 桩中执行顶栏真实 ESM 模块。"""

    assert_node_esm_contract(
        _header_source_for_test(),
        script,
        cwd=ROOT,
        setup=HEADER_SETUP,
    )


def test_header_renders_all_routes_and_dispatches_sidebar_toggle() -> None:
    """顶栏应同步当前页面标题，并通过唯一事件切换侧栏。"""

    _run_header_client(
        r"""
        const container = new __FakeElement();
        client.renderHeader(container);
        assert.equal(container.children.length, 1);
        const header = container.children[0];
        assert.equal(header.tagName, 'header');
        assert.equal(header.className, 'header');
        assert.ok(header.innerHTML.includes('header-logout" type="button"'));
        assert.ok(header.innerHTML.includes('aria-controls="pendo-sidebar"'));
        assert.ok(header.innerHTML.includes('aria-expanded="false"'));

        const title = header.querySelector('.header-title');
        assert.equal(title.textContent, '总览');
        __state.routeCallbacks[0]('transfer');
        assert.equal(title.textContent, '数据迁移');
        __state.routeCallbacks[0]('future-page');
        assert.equal(title.textContent, 'future-page');

        header.querySelector('.header-toggle').emit('click');
        assert.deepEqual(__state.documentEvents, ['pendo:toggle-sidebar']);
        assert.throws(() => client.renderHeader(null), TypeError);
        """
    )


def test_header_search_ignores_ime_and_encodes_trimmed_queries() -> None:
    """组合输入和空查询不应导航，确认后的查询必须编码并清空。"""

    _run_header_client(
        r"""
        const container = new __FakeElement();
        client.renderHeader(container);
        const header = container.children[0];
        const input = header.querySelector('.header-search-input');
        input.value = ' 星 系 & ';

        input.emit('keydown', { key: 'Enter', isComposing: true });
        input.emit('keydown', { key: 'Escape' });
        assert.deepEqual(__state.navigations, []);
        assert.equal(input.value, ' 星 系 & ');

        input.emit('keydown', { key: 'Enter' });
        assert.deepEqual(__state.navigations, ['search?q=%E6%98%9F%20%E7%B3%BB%20%26']);
        assert.equal(input.value, '');

        input.value = '   ';
        input.emit('keydown', { key: 'Enter' });
        header.querySelector('.header-search-toggle').emit('click');
        assert.deepEqual(__state.navigations, [
            'search?q=%E6%98%9F%20%E7%B3%BB%20%26',
            'search',
        ]);
        """
    )


def test_header_logout_prevents_duplicates_and_recovers_from_failure() -> None:
    """退出中应禁用按钮；请求失败后应记录错误、恢复按钮并允许重试。"""

    _run_header_client(
        r"""
        let resolveLogout;
        __state.logoutImpl = () => new Promise((resolve) => { resolveLogout = resolve; });
        const firstContainer = new __FakeElement();
        client.renderHeader(firstContainer);
        const firstButton = firstContainer.children[0].querySelector('.header-logout');

        const firstPending = firstButton.emit('click')[0];
        const duplicatePending = firstButton.emit('click')[0];
        assert.equal(__state.logoutCalls, 1);
        assert.equal(firstButton.disabled, true);
        resolveLogout();
        await Promise.all([firstPending, duplicatePending]);
        assert.equal(__state.reloads, 1);

        __state.logoutImpl = async () => { throw new Error('offline'); };
        const secondContainer = new __FakeElement();
        client.renderHeader(secondContainer);
        const secondButton = secondContainer.children[0].querySelector('.header-logout');
        await secondButton.emit('click')[0];
        assert.equal(__state.logoutCalls, 2);
        assert.equal(__state.reloads, 1);
        assert.equal(secondButton.disabled, false);
        assert.equal(__state.errors.length, 1);
        assert.match(__state.errors[0], /退出登录失败.*offline/);

        __state.logoutImpl = async () => {};
        await secondButton.emit('click')[0];
        assert.equal(__state.logoutCalls, 3);
        assert.equal(__state.reloads, 2);
        """
    )
