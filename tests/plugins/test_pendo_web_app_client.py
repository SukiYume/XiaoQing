"""Pendo Web 浏览器入口的登录、路由和全局交互契约回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
APP_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js"

APP_SETUP: Final = r"""
    globalThis.__state = {
        routes: [],
        routePath: 'tasks',
        routeChangeRegistrations: 0,
        exchangeCalls: [],
        errors: [],
        appended: [],
        loadOrder: [],
        renderOrder: [],
        scrollCalls: [],
        reduceMotion: false,
        replacedUrl: null,
        exchangeBehavior: async () => ({ ok: false, message: '登录失败' }),
        demoBehavior: async () => ({ ok: false, message: '演示失败' }),
        sessionBehavior: async () => ({ ok: false, message: '登录已失效' }),
    };

    class FakeStyle {
        constructor() {
            this.display = '';
            this.properties = {};
        }
        setProperty(name, value) { this.properties[name] = value; }
    }

    class FakeClassList {
        constructor() { this.values = new Set(); }
        contains(name) { return this.values.has(name); }
        toggle(name, force) {
            const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
            if (enabled) this.values.add(name);
            else this.values.delete(name);
            return enabled;
        }
    }

    class FakeElement {
        constructor(id = '') {
            this.id = id;
            this.type = '';
            this.value = '';
            this.textContent = '';
            this.innerHTML = '';
            this.disabled = false;
            this.style = new FakeStyle();
            this.classList = new FakeClassList();
            this.attributes = {};
            this.listeners = {};
            this.focusCount = 0;
            this.onclick = null;
            this.onkeydown = null;
        }
        addEventListener(type, callback) { this.listeners[type] = callback; }
        focus() { this.focusCount += 1; }
        setAttribute(name, value) { this.attributes[name] = value; }
    }

    const elementIds = [
        'login-screen', 'app', 'login-btn', 'login-clear-btn', 'login-demo-btn',
        'token-input', 'login-error', 'login-helper', 'sidebar-container',
        'header-container', 'content',
    ];
    globalThis.__elements = new Map(elementIds.map((id) => [id, new FakeElement(id)]));
    const body = new FakeElement('body');
    body.appendChild = (element) => {
        __state.appended.push(element);
        __elements.set(element.id, element);
        return element;
    };
    globalThis.document = {
        title: 'Pendo Web',
        body,
        createElement: () => new FakeElement(),
        getElementById: (id) => __elements.get(id) || null,
    };

    globalThis.window = {
        location: { href: 'https://pendo.example/' },
        history: {
            replaceState: (_state, _title, url) => { __state.replacedUrl = url; },
        },
        scrollY: 0,
        listeners: {},
        addEventListener(type, callback) { this.listeners[type] = callback; },
        matchMedia: () => ({ matches: __state.reduceMotion }),
        scrollTo: (options) => { __state.scrollCalls.push(options); },
    };
    globalThis.console.error = (...args) => {
        __state.errors.push(args.map((value) => String(value)).join(' '));
    };

    globalThis.__api = {
        createDemoSession: () => __state.demoBehavior(),
        exchangeLoginCode: (code) => {
            __state.exchangeCalls.push(code);
            return __state.exchangeBehavior(code);
        },
        getSession: () => __state.sessionBehavior(),
    };
    globalThis.__router = {
        init: async (container) => {
            __state.routerContainer = container.id;
            __state.routerInitDisplay = __elements.get('app').style.display;
        },
        onRouteChange: (callback) => {
            __state.routeChangeRegistrations += 1;
            __state.routeCallback = callback;
            callback(__state.routePath);
        },
        registerRoute: (path, loader) => { __state.routes.push({ path, loader }); },
    };
    globalThis.__loadSidebar = async () => {
        __state.loadOrder.push('sidebar');
        return {
            renderSidebar: (container) => { __state.renderOrder.push(`sidebar:${container.id}`); },
        };
    };
    globalThis.__loadHeader = async () => {
        __state.loadOrder.push('header');
        return {
            renderHeader: (container) => { __state.renderOrder.push(`header:${container.id}`); },
        };
    };
"""


def _app_source_for_test() -> str:
    """把静态依赖替换为测试桩，并导出入口内部的可验证边界。"""

    source = APP_CLIENT.read_text(encoding="utf-8")
    replacements = (
        (
            "import { createDemoSession, exchangeLoginCode, getSession } from './api.js';",
            "const { createDemoSession, exchangeLoginCode, getSession } = globalThis.__api;",
        ),
        (
            "import { init as initRouter, onRouteChange, registerRoute } from './router.js';",
            "const { init: initRouter, onRouteChange, registerRoute } = globalThis.__router;",
        ),
        ("import('./components/sidebar.js')", "globalThis.__loadSidebar()"),
        ("import('./components/header.js')", "globalThis.__loadHeader()"),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    bootstrap_call = "\nbootstrap().catch((cause) => {"
    assert bootstrap_call in source
    source = source[: source.index(bootstrap_call)]
    return source + "\nexport { bootstrap, extractLoginCode, initBackToTop, showApp, showLogin };\n"


def _run_app_client(script: str) -> None:
    """在最小浏览器桩上执行真实入口模块。"""

    assert_node_esm_contract(
        _app_source_for_test(),
        script,
        cwd=ROOT,
        setup=APP_SETUP,
    )


def test_app_registers_routes_extracts_codes_and_consumes_url_login_code() -> None:
    """入口应保持确定性路由顺序，并在显示错误前从地址栏安全消费一次性登录码。"""

    _run_app_client(
        r"""
        assert.deepEqual(
            __state.routes.map((route) => route.path),
            ['dashboard', 'events', 'tasks', 'ledger', 'notes', 'diary', 'search', 'stats', 'settings', 'transfer'],
        );

        const code = 'Abcd_1234-Efgh_5678-Ijkl';
        assert.equal(client.extractLoginCode(`  ${code}  `), code);
        assert.equal(client.extractLoginCode(`https://pendo.example/?code=${code}`), code);
        assert.equal(client.extractLoginCode(`请打开 https://pendo.example/?code=${code} 后登录`), code);
        assert.equal(client.extractLoginCode('https://pendo.example/no-code'), '');
        assert.equal(client.extractLoginCode('  '), '');

        __state.exchangeBehavior = async () => ({ ok: false, message: '链接已过期' });
        window.location.href = `https://pendo.example/?code=${code}&from=chat#/tasks`;
        await client.bootstrap();

        assert.deepEqual(__state.exchangeCalls, [code]);
        assert.equal(__state.replacedUrl, '/?from=chat#/tasks');
        assert.equal(__elements.get('login-screen').style.display, 'flex');
        assert.equal(__elements.get('app').style.display, 'none');
        assert.equal(__elements.get('login-error').textContent, '链接已过期');
        """
    )


def test_bootstrap_hides_expected_unauthenticated_error_but_keeps_real_failures() -> None:
    """匿名 401 应安静展示登录页，连接或服务异常仍必须提示用户。"""

    _run_app_client(
        r"""
        const error = __elements.get('login-error');

        __state.sessionBehavior = async () => ({
            ok: false,
            message: 'Missing web session',
            httpStatus: 401,
        });
        await client.bootstrap();
        assert.equal(__elements.get('login-screen').style.display, 'flex');
        assert.equal(error.textContent, '');
        assert.equal(error.style.display, 'none');

        __state.sessionBehavior = async () => ({
            ok: false,
            message: '无法连接到 Web 服务',
            httpStatus: null,
        });
        await client.bootstrap();
        assert.equal(error.textContent, '无法连接到 Web 服务');
        assert.equal(error.style.display, 'block');

        __state.sessionBehavior = async () => ({
            ok: false,
            message: '服务暂时不可用',
            httpStatus: 503,
        });
        await client.bootstrap();
        assert.equal(error.textContent, '服务暂时不可用');
        assert.equal(error.style.display, 'block');
        """
    )


def test_login_actions_are_single_flight_and_restore_every_control() -> None:
    """登录与 Demo 请求应串行执行，期间锁定全部控件，并在失败或异常后完整恢复。"""

    _run_app_client(
        r"""
        client.showLogin();
        const loginButton = __elements.get('login-btn');
        const clearButton = __elements.get('login-clear-btn');
        const demoButton = __elements.get('login-demo-btn');
        const input = __elements.get('token-input');
        const error = __elements.get('login-error');
        const helper = __elements.get('login-helper');
        const code = 'Abcd_1234-Efgh_5678-Ijkl';

        let resolveExchange;
        __state.exchangeBehavior = () => new Promise((resolve) => { resolveExchange = resolve; });
        input.value = `https://pendo.example/?code=${code}`;
        const firstLogin = loginButton.onclick();
        const duplicateLogin = loginButton.onclick();
        assert.equal(__state.exchangeCalls.length, 1);
        assert.ok([loginButton, clearButton, demoButton, input].every((control) => control.disabled));
        assert.equal(input.value, code);
        resolveExchange({ ok: false, message: '登录码无效' });
        await Promise.all([firstLogin, duplicateLogin]);
        assert.ok([loginButton, clearButton, demoButton, input].every((control) => !control.disabled));
        assert.equal(loginButton.textContent, '进入 Pendo');
        assert.equal(error.textContent, '登录码无效');

        let resolveDemo;
        __state.demoBehavior = () => new Promise((resolve) => { resolveDemo = resolve; });
        const demoRequest = demoButton.onclick();
        assert.ok([loginButton, clearButton, demoButton, input].every((control) => control.disabled));
        resolveDemo({ ok: false, message: '演示空间已满' });
        await demoRequest;
        assert.ok([loginButton, clearButton, demoButton, input].every((control) => !control.disabled));
        assert.equal(error.textContent, '演示空间已满');

        __state.exchangeBehavior = async () => { throw new Error('component failed'); };
        input.value = code;
        await loginButton.onclick();
        assert.equal(error.textContent, '登录时发生错误，请稍后重试');
        assert.match(helper.textContent, /刷新页面/);
        assert.equal(__state.errors.length, 1);

        clearButton.onclick();
        assert.equal(input.value, '');
        assert.equal(error.style.display, 'none');
        assert.ok(input.focusCount >= 2);
        """
    )


def test_show_app_initializes_layout_and_one_accessible_back_to_top_button() -> None:
    """主界面应并行装载布局，并只建立一个尊重路由主题与减弱动画偏好的顶部按钮。"""

    _run_app_client(
        r"""
        await client.showApp();
        assert.deepEqual(__state.loadOrder, ['sidebar', 'header']);
        assert.deepEqual(__state.renderOrder, ['sidebar:sidebar-container', 'header:header-container']);
        assert.equal(__elements.get('login-screen').style.display, 'none');
        assert.equal(__elements.get('app').style.display, 'flex');
        assert.equal(__state.routerContainer, 'content');
        assert.equal(__state.routerInitDisplay, 'flex');

        const button = __elements.get('back-to-top');
        assert.ok(button);
        assert.equal(button.type, 'button');
        assert.equal(button.attributes['aria-label'], '回到顶部');
        assert.equal(button.style.properties['--btt-accent'], 'var(--color-tasks)');
        assert.equal(__state.appended.length, 1);
        assert.equal(__state.routeChangeRegistrations, 1);

        client.initBackToTop();
        assert.equal(__state.appended.length, 1);
        assert.equal(__state.routeChangeRegistrations, 1);

        window.scrollY = 300;
        window.listeners.scroll();
        assert.equal(button.classList.contains('btt-visible'), true);
        __state.routeCallback('notes');
        assert.equal(button.style.properties['--btt-accent'], 'var(--color-notes)');

        button.listeners.click();
        __state.reduceMotion = true;
        button.listeners.click();
        assert.deepEqual(__state.scrollCalls, [
            { top: 0, behavior: 'smooth' },
            { top: 0, behavior: 'auto' },
        ]);
        """
    )
