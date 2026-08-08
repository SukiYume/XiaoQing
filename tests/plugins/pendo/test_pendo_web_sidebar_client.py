"""Pendo Web 侧栏的导航结构、路由和移动端抽屉回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
SIDEBAR_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "sidebar.js"
)

SIDEBAR_SETUP: Final = r"""
    class FakeClassList {
        constructor() { this.values = new Set(); }
        add(...names) { names.forEach((name) => this.values.add(name)); }
        remove(...names) { names.forEach((name) => this.values.delete(name)); }
        contains(name) { return this.values.has(name); }
        toggle(name, force) {
            const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
            if (enabled) this.values.add(name);
            else this.values.delete(name);
            return enabled;
        }
    }

    class FakeElement {
        constructor(tagName = 'div') {
            this.tagName = String(tagName).toLowerCase();
            this.id = '';
            this.type = '';
            this.href = '';
            this.className = '';
            this.classList = new FakeClassList();
            this.textContent = '';
            this.innerHTML = '';
            this.tabIndex = 0;
            this.dataset = {};
            this.children = [];
            this.attributes = new Map();
            this.listeners = new Map();
            this.focusCount = 0;
            this.style = {
                values: new Map(),
                setProperty: (name, value) => this.style.values.set(name, String(value)),
            };
        }
        append(...children) { this.children.push(...children); }
        appendChild(child) { this.children.push(child); return child; }
        replaceChildren(...children) { this.children = children; }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.get(name) ?? null; }
        removeAttribute(name) { this.attributes.delete(name); }
        addEventListener(type, callback) {
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(callback);
        }
        emit(type, init = {}) {
            const event = { target: init.target || this };
            for (const callback of this.listeners.get(type) || []) callback(event);
            return event;
        }
        click() { return this.emit('click'); }
        focus() {
            this.focusCount += 1;
            document.activeElement = this;
        }
        descendants() {
            const found = [];
            const visit = (node) => {
                for (const child of node.children) {
                    found.push(child);
                    visit(child);
                }
            };
            visit(this);
            return found;
        }
        querySelectorAll(selector) {
            if (!selector.startsWith('.')) return [];
            const className = selector.slice(1);
            return this.descendants().filter((node) =>
                node.className.split(/\s+/).includes(className),
            );
        }
        contains(node) { return node === this || this.descendants().includes(node); }
    }

    const documentListeners = new Map();
    const headerToggle = new FakeElement('button');
    headerToggle.className = 'header-toggle';
    const documentBody = new FakeElement('body');
    globalThis.document = {
        body: documentBody,
        activeElement: headerToggle,
        createElement: (tagName) => new FakeElement(tagName),
        querySelector: (selector) => selector === '.header-toggle' ? headerToggle : null,
        addEventListener(type, callback) {
            if (!documentListeners.has(type)) documentListeners.set(type, []);
            documentListeners.get(type).push(callback);
        },
        emit(type, init = {}) {
            const event = {
                key: init.key,
                isComposing: Boolean(init.isComposing),
                defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
            };
            for (const callback of documentListeners.get(type) || []) callback(event);
            return event;
        },
        listenerCount(type) { return documentListeners.get(type)?.length || 0; },
    };

    const mediaListeners = [];
    const mobileMedia = {
        matches: false,
        addEventListener(type, callback) {
            if (type === 'change') mediaListeners.push(callback);
        },
        emit(matches) {
            this.matches = matches;
            mediaListeners.forEach((callback) => callback({ matches }));
        },
        listenerCount() { return mediaListeners.length; },
    };
    globalThis.window = { matchMedia: () => mobileMedia };
    globalThis.__router = {
        callbacks: [],
        onRouteChange(callback) {
            globalThis.__router.callbacks.push(callback);
            callback('dashboard');
            return () => {};
        },
    };
    globalThis.__FakeElement = FakeElement;
    globalThis.__headerToggle = headerToggle;
    globalThis.__mobileMedia = mobileMedia;
"""


def _sidebar_source_for_test() -> str:
    """只替换路由依赖，保留侧栏模块真实实现。"""

    source = SIDEBAR_CLIENT.read_text(encoding="utf-8")
    import_line = "import { onRouteChange } from '../router.js';"
    assert import_line in source
    return source.replace(import_line, "const { onRouteChange } = globalThis.__router;")


def _run_sidebar_client(script: str) -> None:
    """在最小 DOM 和媒体查询桩中执行侧栏真实 ESM 模块。"""

    assert_node_esm_contract(
        _sidebar_source_for_test(),
        script,
        cwd=ROOT,
        setup=SIDEBAR_SETUP,
    )


def test_sidebar_builds_sectioned_native_navigation_and_tracks_current_route() -> None:
    """导航应由两段配置生成，并只标记一个当前页面。"""

    _run_sidebar_client(
        r"""
        const container = new __FakeElement();
        client.renderSidebar(container);
        assert.equal(container.children.length, 2);
        const [backdrop, sidebar] = container.children;
        assert.equal(backdrop.tagName, 'button');
        assert.equal(backdrop.type, 'button');
        assert.equal(sidebar.tagName, 'aside');
        assert.equal(sidebar.id, 'pendo-sidebar');
        assert.equal(sidebar.getAttribute('aria-label'), '主要导航');
        assert.equal(sidebar.getAttribute('aria-hidden'), 'false');

        const links = sidebar.querySelectorAll('.nav-item');
        assert.equal(links.length, 9);
        assert.equal(sidebar.querySelectorAll('.nav-section-label').length, 2);
        const separators = sidebar.querySelectorAll('.sidebar-separator');
        assert.equal(separators.length, 1);
        assert.equal(separators[0].getAttribute('role'), 'separator');
        assert.equal(links[0].href, '#/dashboard');
        assert.equal(links[0].dataset.module, 'dashboard');
        assert.equal(links[0].style.values.get('--nav-accent'), 'var(--color-dashboard)');
        assert.equal(links[0].children[0].children[0].getAttribute('aria-hidden'), 'true');
        assert.equal(links[0].classList.contains('active'), true);
        assert.equal(links[0].getAttribute('aria-current'), 'page');

        __router.callbacks[0]('settings');
        assert.equal(links.filter((link) => link.classList.contains('active')).length, 1);
        assert.equal(links.at(-1).classList.contains('active'), true);
        assert.equal(links.at(-1).getAttribute('aria-current'), 'page');
        assert.equal(links[0].getAttribute('aria-current'), null);
        assert.throws(() => client.renderSidebar(null), TypeError);
        """
    )


def test_sidebar_mobile_toggle_escape_and_backdrop_keep_aria_in_sync() -> None:
    """移动抽屉开关应同步视觉、滚动锁、焦点和头部按钮状态。"""

    _run_sidebar_client(
        r"""
        __mobileMedia.matches = true;
        const container = new __FakeElement();
        client.renderSidebar(container);
        const [backdrop, sidebar] = container.children;
        assert.equal(sidebar.getAttribute('aria-hidden'), 'true');
        assert.equal(backdrop.tabIndex, -1);
        assert.equal(__headerToggle.getAttribute('aria-expanded'), 'false');

        document.emit('pendo:toggle-sidebar');
        assert.equal(sidebar.classList.contains('mobile-open'), true);
        assert.equal(sidebar.getAttribute('aria-hidden'), 'false');
        assert.equal(backdrop.classList.contains('visible'), true);
        assert.equal(backdrop.tabIndex, 0);
        assert.equal(backdrop.getAttribute('aria-hidden'), 'false');
        assert.equal(document.body.classList.contains('sidebar-open'), true);
        assert.equal(__headerToggle.getAttribute('aria-expanded'), 'true');
        assert.equal(__headerToggle.getAttribute('aria-label'), '关闭导航菜单');

        let event = document.emit('keydown', { key: 'Escape', isComposing: true });
        assert.equal(event.defaultPrevented, false);
        assert.equal(sidebar.classList.contains('mobile-open'), true);
        event = document.emit('keydown', { key: 'Escape' });
        assert.equal(event.defaultPrevented, true);
        assert.equal(sidebar.classList.contains('mobile-open'), false);
        assert.equal(__headerToggle.focusCount, 1);

        document.emit('pendo:toggle-sidebar');
        backdrop.click();
        assert.equal(sidebar.classList.contains('mobile-open'), false);
        assert.equal(__headerToggle.focusCount, 2);
        assert.equal(__headerToggle.getAttribute('aria-expanded'), 'false');
        assert.equal(__headerToggle.getAttribute('aria-label'), '打开导航菜单');

        document.emit('pendo:toggle-sidebar');
        document.activeElement = sidebar.querySelectorAll('.nav-item')[2];
        __router.callbacks[0]('tasks');
        assert.equal(sidebar.classList.contains('mobile-open'), false);
        assert.equal(document.body.classList.contains('sidebar-open'), false);
        assert.equal(__headerToggle.focusCount, 3);
        """
    )


def test_sidebar_uses_one_media_change_listener_instead_of_resize_width_checks() -> None:
    """媒体条件变化应关闭抽屉并同步桌面/移动可访问状态。"""

    _run_sidebar_client(
        r"""
        __mobileMedia.matches = true;
        const container = new __FakeElement();
        client.renderSidebar(container);
        const [, sidebar] = container.children;
        document.emit('pendo:toggle-sidebar');
        assert.equal(sidebar.classList.contains('mobile-open'), true);

        __mobileMedia.emit(false);
        assert.equal(sidebar.classList.contains('mobile-open'), false);
        assert.equal(sidebar.getAttribute('aria-hidden'), 'false');
        document.emit('pendo:toggle-sidebar');
        assert.equal(sidebar.classList.contains('mobile-open'), false);

        __mobileMedia.emit(true);
        assert.equal(sidebar.getAttribute('aria-hidden'), 'true');
        assert.equal(__mobileMedia.listenerCount(), 1);
        assert.equal(document.listenerCount('pendo:toggle-sidebar'), 1);
        assert.equal(document.listenerCount('keydown'), 1);
        """
    )
