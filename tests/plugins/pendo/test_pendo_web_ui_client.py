"""Pendo Web UI 共享工具的转义、样式和数据订阅回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final      = REPOSITORY_ROOT
UI_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "ui.js"

UI_SETUP: Final = r"""
    globalThis.__state = {
        appended: [],
        createCount: 0,
        removeCount: 0,
    };
    globalThis.__elements = new Map();

    class FakeStyleElement {
        constructor() {
            this.id = '';
            this.tagName = 'STYLE';
            this._textContent = '';
            this.writeCount = 0;
        }
        get textContent() { return this._textContent; }
        set textContent(value) {
            this._textContent = value;
            this.writeCount += 1;
        }
    }

    globalThis.document = {
        getElementById: (id) => __elements.get(id) || null,
        createElement: (tagName) => {
            assert.equal(tagName, 'style');
            __state.createCount += 1;
            return new FakeStyleElement();
        },
        head: {
            appendChild(element) {
                __state.appended.push(element);
                __elements.set(element.id, element);
                return element;
            },
        },
    };

    const listeners = new Map();
    globalThis.window = {
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(callback);
        },
        removeEventListener(type, callback) {
            __state.removeCount += 1;
            listeners.get(type)?.delete(callback);
        },
        emit(type, event) {
            for (const callback of [...(listeners.get(type) || [])]) callback(event);
        },
    };
"""


def _run_ui_client(script: str) -> None:
    """在最小 DOM 与事件桩上导入真实 UI 工具模块。"""

    assert_node_esm_contract(
        UI_CLIENT.read_text(encoding="utf-8"),
        script,
        cwd   = ROOT,
        setup = UI_SETUP,
    )


def test_ui_client_escapes_markup_and_exposes_only_live_breakpoints() -> None:
    """文本与属性共用安全转义，响应式常量只保留三个真实消费者。"""

    _run_ui_client(
        r"""
        assert.equal(client.escapeHtml(null), '');
        assert.equal(client.escapeHtml('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
        assert.equal(client.escapeHtml(0), '0');
        assert.equal(client.escapeAttr, client.escapeHtml);

        assert.deepEqual(client.BREAKPOINTS, {
            XL: '1200px', MOBILE: '720px', PHONE: '560px',
        });
        assert.equal(Object.isFrozen(client.BREAKPOINTS), true);
        assert.throws(() => { client.BREAKPOINTS.XL = '1px'; }, TypeError);
        assert.equal(
            client.mediaMax(client.BREAKPOINTS.PHONE, '.x { display: none; }'),
            '@media (max-width: 560px) { .x { display: none; } }',
        );

        const defaults = client.pageShellCss('demo');
        assert.match(defaults, /\.demo \{/);
        assert.match(defaults, /max-width: 1280px/);
        assert.match(defaults, /padding: 26px 24px 36px/);
        assert.ok(!defaults.includes('@media'));

        const compact = client.pageShellCss('demo', {
            padding: '1px', maxWidth: '900px', margin: '0', compactPadding: '2px',
        });
        assert.match(compact, /max-width: 900px/);
        assert.match(compact, /margin: 0/);
        assert.match(compact, /padding: 1px/);
        assert.match(compact, /@media \(max-width: 720px\)/);
        assert.match(compact, /\.demo \{ padding: 2px; \}/);
        """
    )


def test_ui_client_injects_one_style_node_and_updates_only_changed_css() -> None:
    """同一页面重复渲染不能累积样式节点或重复写入相同文本。"""

    _run_ui_client(
        r"""
        client.injectStyles('page-css', '.page { color: red; }');
        const style = __elements.get('page-css');
        assert.ok(style);
        assert.equal(style.textContent, '.page { color: red; }');
        assert.equal(style.writeCount, 1);
        assert.equal(__state.createCount, 1);
        assert.equal(__state.appended.length, 1);

        client.injectStyles('page-css', '.page { color: red; }');
        assert.equal(style.writeCount, 1);
        assert.equal(__state.appended.length, 1);

        client.injectStyles('page-css', '.page { color: blue; }');
        assert.equal(style.textContent, '.page { color: blue; }');
        assert.equal(style.writeCount, 2);
        assert.equal(__state.createCount, 1);
        """
    )


def test_ui_client_filters_data_events_and_unsubscribes_idempotently() -> None:
    """带类型事件只刷新对应页面，无类型事件全局刷新，重复退订只移除一次。"""

    _run_ui_client(
        r"""
        const taskEvents = [];
        const allEvents = [];
        const unsubscribeTask = client.subscribeDataChanges(
            'task', (event) => taskEvents.push(event?.detail?.type || 'global'),
        );
        const unsubscribeAll = client.subscribeDataChanges(
            null, (event) => allEvents.push(event?.detail?.type || 'global'),
        );

        window.emit('pendo-data-changed', { detail: { type: 'note' } });
        window.emit('pendo-data-changed', { detail: { type: 'task' } });
        window.emit('pendo-data-changed', { detail: {} });
        assert.deepEqual(taskEvents, ['task', 'global']);
        assert.deepEqual(allEvents, ['note', 'task', 'global']);

        unsubscribeTask();
        unsubscribeTask();
        window.emit('pendo-data-changed', { detail: { type: 'task' } });
        assert.deepEqual(taskEvents, ['task', 'global']);
        assert.deepEqual(allEvents, ['note', 'task', 'global', 'task']);
        assert.equal(__state.removeCount, 1);

        unsubscribeAll();
        window.emit('pendo-data-changed', { detail: {} });
        assert.deepEqual(allEvents, ['note', 'task', 'global', 'task']);
        assert.equal(__state.removeCount, 2);
        """
    )
