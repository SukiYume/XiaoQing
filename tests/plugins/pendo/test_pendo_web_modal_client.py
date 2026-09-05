"""Pendo Web 模态框的内容边界、焦点和关闭生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final         = REPOSITORY_ROOT
MODAL_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "modal.js"
)

MODAL_SETUP: Final = r"""
    globalThis.__state = { templateHtml: [], errors: [] };
    globalThis.console.error = (...args) => {
        __state.errors.push(args.map((value) => String(value)).join(' '));
    };
    globalThis.__ui = {
        escapeHtml: (value) => String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;'),
    };

    class FakeClassList {
        constructor() { this.values = new Set(); }
        add(...names) { names.forEach((name) => this.values.add(name)); }
        remove(...names) { names.forEach((name) => this.values.delete(name)); }
        contains(name) { return this.values.has(name); }
    }

    class FakeNode {
        constructor() {
            this.parentElement = null;
            this.isConnected = false;
        }
    }

    const connectTree = (node, connected) => {
        node.isConnected = connected;
        for (const child of node.children || []) connectTree(child, connected);
    };

    const parseButtons = (html) => {
        const fragment = { isFragment: true, children: [] };
        const pattern = /<button\b([^>]*)>([\s\S]*?)<\/button>/gi;
        for (const match of html.matchAll(pattern)) {
            const button = new FakeElement('button');
            const attrs = match[1];
            button.id = attrs.match(/\bid="([^"]*)"/)?.[1] || '';
            button.className = attrs.match(/\bclass="([^"]*)"/)?.[1] || '';
            button.type = attrs.match(/\btype="([^"]*)"/)?.[1] || '';
            button.textContent = match[2].replace(/<[^>]*>/g, '');
            fragment.children.push(button);
        }
        return fragment;
    };

    class FakeElement extends FakeNode {
        constructor(tagName = 'div') {
            super();
            this.tagName = String(tagName).toLowerCase();
            this.children = [];
            this.style = {};
            this.className = '';
            this.classList = new FakeClassList();
            this.textContent = '';
            this.id = '';
            this.type = '';
            this.disabled = false;
            this.hidden = false;
            this.attributes = new Map();
            this.onclick = null;
            this.focusCount = 0;
            this._innerHTML = '';
            if (this.tagName === 'template') {
                this.content = { cloneNode: () => parseButtons(this._innerHTML) };
            }
        }
        set innerHTML(value) {
            this._innerHTML = String(value);
            if (this.tagName === 'template') __state.templateHtml.push(this._innerHTML);
        }
        get innerHTML() { return this._innerHTML; }
        append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
        appendChild(node) {
            if (node?.isFragment) {
                for (const child of node.children) this.appendChild(child);
                return node;
            }
            node.parentElement = this;
            this.children.push(node);
            connectTree(node, this.isConnected);
            return node;
        }
        replaceChildren(...nodes) {
            for (const child of this.children) connectTree(child, false);
            this.children = [];
            this.append(...nodes);
        }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.get(name) ?? null; }
        removeAttribute(name) { this.attributes.delete(name); }
        matches(selector) {
            if (selector.startsWith('#')) return this.id === selector.slice(1);
            if (selector.startsWith('.')) {
                return this.className.split(/\s+/).includes(selector.slice(1));
            }
            return false;
        }
        descendants() {
            const result = [];
            const visit = (node) => {
                for (const child of node.children) {
                    result.push(child);
                    visit(child);
                }
            };
            visit(this);
            return result;
        }
        querySelector(selector) {
            return this.descendants().find((node) => node.matches(selector)) || null;
        }
        querySelectorAll(selector) {
            const nodes = this.descendants();
            if (selector.includes('button:not([disabled])')) {
                const focusableTags = new Set(['button', 'a', 'input', 'select', 'textarea']);
                return nodes.filter((node) =>
                    !node.disabled
                    && !node.hidden
                    && (focusableTags.has(node.tagName)
                        || (node.getAttribute('tabindex') !== null
                            && node.getAttribute('tabindex') !== '-1')),
                );
            }
            return nodes.filter((node) => node.matches(selector));
        }
        focus() {
            this.focusCount += 1;
            document.activeElement = this;
        }
        click(target = this) { return this.onclick?.({ target }); }
    }

    const listeners = new Map();
    const overlay = new FakeElement('div');
    overlay.id = 'modal-overlay';
    overlay.style.display = 'none';
    const content = new FakeElement('div');
    content.id = 'modal-content';
    overlay.appendChild(content);
    connectTree(overlay, true);
    const documentBody = new FakeElement('body');
    connectTree(documentBody, true);
    const trigger = new FakeElement('button');
    trigger.id = 'trigger';
    connectTree(trigger, true);

    globalThis.document = {
        body: documentBody,
        activeElement: trigger,
        createElement: (tagName) => new FakeElement(tagName),
        getElementById(id) {
            if (id === 'modal-overlay') return overlay;
            if (id === 'modal-content') return content;
            return null;
        },
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(callback);
        },
        removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
        emit(type, init = {}) {
            const event = {
                key: init.key,
                shiftKey: Boolean(init.shiftKey),
                isComposing: Boolean(init.isComposing),
                defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
            };
            for (const callback of [...(listeners.get(type) || [])]) callback(event);
            return event;
        },
        listenerCount(type) { return listeners.get(type)?.size || 0; },
    };
    globalThis.Node = FakeNode;
    globalThis.__FakeElement = FakeElement;
    globalThis.__overlay = overlay;
    globalThis.__content = content;
    globalThis.__trigger = trigger;
"""


def _modal_source_for_test() -> str:
    """只替换转义依赖，保留模态框真实实现。"""

    source = MODAL_CLIENT.read_text(encoding="utf-8")
    import_line = "import { escapeHtml } from '../utils/ui.js';"
    assert import_line in source
    return source.replace(import_line, "const { escapeHtml } = globalThis.__ui;")


def _run_modal_client(script: str) -> None:
    """在最小 DOM 桩中执行模态框真实 ESM 模块。"""

    assert_node_esm_contract(
        _modal_source_for_test(),
        script,
        cwd   = ROOT,
        setup = MODAL_SETUP,
    )


def test_modal_requires_branded_content_and_restores_trigger_state() -> None:
    """普通字符串必须拒绝，打开和关闭应同步标题、滚动锁、监听器与焦点。"""

    _run_modal_client(
        r"""
        const attack = '<img src=x onerror="bad()">';
        assert.throws(() => client.safeHtml(1), TypeError);
        assert.throws(() => client.showModal('unsafe', attack), TypeError);
        assert.throws(() => client.showModal('bad options', client.safeHtml(''), []), TypeError);
        assert.throws(
            () => client.showModal('bad callback', client.safeHtml(''), { onClose: true }),
            TypeError,
        );
        assert.throws(() => client.showConfirmModal([]), TypeError);

        const branded = client.safeHtml('<button type="button" id="footer-action">确定</button>');
        assert.equal(Object.isFrozen(branded), true);
        const bodyNode = new __FakeElement('p');
        bodyNode.textContent = attack;
        const renderedContent = client.showModal(attack, bodyNode, { footer: branded });
        assert.equal(renderedContent, __content);
        assert.equal(renderedContent.querySelector('#modal-title').textContent, attack);
        assert.equal(renderedContent.children[1].children[0], bodyNode);
        assert.equal(renderedContent.querySelector('#footer-action').type, 'button');
        assert.equal(__overlay.style.display, 'flex');
        assert.equal(__overlay.getAttribute('aria-labelledby'), 'modal-title');
        assert.equal(document.body.classList.contains('modal-open'), true);
        assert.equal(document.listenerCount('keydown'), 1);
        assert.equal(renderedContent.querySelector('.modal-close').focusCount, 1);
        assert.throws(
            () => client.showModal('duplicate', client.safeHtml('<p>重复</p>')),
            /已有模态框打开/,
        );

        client.closeModal();
        assert.equal(__overlay.style.display, 'none');
        assert.equal(__overlay.getAttribute('aria-labelledby'), null);
        assert.equal(__content.children.length, 0);
        assert.equal(document.body.classList.contains('modal-open'), false);
        assert.equal(document.listenerCount('keydown'), 0);
        assert.equal(__trigger.focusCount, 1);
        client.closeModal();
        assert.equal(__trigger.focusCount, 1);
        """
    )


def test_modal_traps_focus_and_preserves_reentrant_close_callbacks() -> None:
    """Tab、背景点击和 Escape 应关闭同一实例，关闭回调可安全打开下一实例。"""

    _run_modal_client(
        r"""
        const bodyNode = new __FakeElement('div');
        const firstBodyButton = new __FakeElement('button');
        const lastBodyButton = new __FakeElement('button');
        bodyNode.append(firstBodyButton, lastBodyButton);
        const calls = [];
        client.showModal('first', bodyNode, {
            onClose: () => {
                calls.push('first');
                client.showModal('second', client.safeHtml('<p>next</p>'), {
                    onClose: () => calls.push('second'),
                });
            },
        });
        const closeButton = __content.querySelector('.modal-close');
        const focusable = __content.querySelectorAll('button:not([disabled])');
        assert.equal(focusable[0], closeButton);
        assert.equal(focusable.at(-1), lastBodyButton);

        lastBodyButton.focus();
        let event = document.emit('keydown', { key: 'Tab' });
        assert.equal(event.defaultPrevented, true);
        assert.equal(document.activeElement, closeButton);
        event = document.emit('keydown', { key: 'Tab', shiftKey: true });
        assert.equal(event.defaultPrevented, true);
        assert.equal(document.activeElement, lastBodyButton);

        __overlay.click(__content);
        assert.equal(__overlay.style.display, 'flex');
        __overlay.click(__overlay);
        assert.deepEqual(calls, ['first']);
        assert.equal(__content.querySelector('#modal-title').textContent, 'second');
        assert.equal(document.body.classList.contains('modal-open'), true);
        client.closeModal();
        assert.deepEqual(calls, ['first', 'second']);

        client.showModal('error', client.safeHtml('<p>body</p>'), {
            onClose: () => { throw new Error('close failed'); },
        });
        client.closeModal();
        assert.equal(__state.errors.length, 1);
        assert.match(__state.errors[0], /关闭回调失败.*close failed/);

        client.showModal('async error', client.safeHtml('<p>body</p>'), {
            onClose: async () => { throw new Error('async close failed'); },
        });
        client.closeModal();
        await Promise.resolve();
        await Promise.resolve();
        assert.equal(__state.errors.length, 2);
        assert.match(__state.errors[1], /关闭回调失败.*async close failed/);

        client.showModal('escape', client.safeHtml('<p>body</p>'));
        document.emit('keydown', { key: 'Escape', isComposing: true });
        assert.equal(__overlay.style.display, 'flex');
        event = document.emit('keydown', { key: 'Escape' });
        assert.equal(event.defaultPrevented, true);
        assert.equal(__overlay.style.display, 'none');
        """
    )


def test_confirm_modal_settles_once_and_escapes_all_visible_values() -> None:
    """确认、取消和 Escape 应各自只结算一次，HTML 中的文案必须转义。"""

    _run_modal_client(
        r"""
        const attack = '</p><script>bad()</script>';
        const cancelled = client.showConfirmModal({
            title: attack,
            message: attack,
            cancelText: attack,
            confirmText: attack,
            tone: 'info',
        });
        assert.equal(__content.querySelector('#modal-title').textContent, attack);
        const cancelButton = __content.querySelector('#confirm-cancel');
        assert.equal(cancelButton.type, 'button');
        cancelButton.click();
        cancelButton.click();
        assert.equal(await cancelled, false);

        const generatedHtml = __state.templateHtml.join('\n');
        assert.ok(generatedHtml.includes('&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;'));
        assert.ok(!generatedHtml.includes('<script>'));
        assert.ok(generatedHtml.includes('confirm-modal-icon-info'));
        assert.ok(!generatedHtml.includes('style='));

        const confirmed = client.showConfirmModal({ tone: 'danger' });
        __content.querySelector('#confirm-ok').click();
        assert.equal(await confirmed, true);
        assert.ok(__state.templateHtml.join('\n').includes('confirm-modal-icon-danger'));

        const escaped = client.showConfirmModal();
        document.emit('keydown', { key: 'Escape' });
        assert.equal(await escaped, false);
        """
    )
