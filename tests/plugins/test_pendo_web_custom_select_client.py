"""Pendo Web 自定义选择框的渲染、键盘和回调生命周期回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
CUSTOM_SELECT_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "custom_select.js"
)

CUSTOM_SELECT_SETUP: Final = r"""
    globalThis.__errors = [];
    globalThis.console.error = (...args) => {
        __errors.push(args.map((value) => String(value)).join(' '));
    };
    globalThis.__ui = {
        escapeHtml: (value) => String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;'),
    };
    __ui.escapeAttr = __ui.escapeHtml;

    class FakeClassList {
        constructor(values = []) { this.values = new Set(values); }
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

    const matches = (element, selector) => {
        if (!selector.startsWith('.')) return false;
        return selector.slice(1).split('.').every((name) => element.classList.contains(name));
    };

    class FakeElement {
        constructor({ id = '', classes = [], text = '', value = '' } = {}) {
            this.id = id;
            this.classList = new FakeClassList(classes);
            this.textContent = text;
            this.value = value;
            this.dataset = {};
            this.attributes = new Map();
            this.children = [];
            this.parentElement = null;
            this.listeners = new Map();
            this.focusCount = 0;
        }
        append(...children) {
            for (const child of children) {
                child.parentElement = this;
                this.children.push(child);
            }
        }
        querySelectorAll(selector) {
            const found = [];
            const visit = (element) => {
                for (const child of element.children) {
                    if (matches(child, selector)) found.push(child);
                    visit(child);
                }
            };
            visit(this);
            return found;
        }
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
        closest(selector) {
            let current = this;
            while (current) {
                if (matches(current, selector)) return current;
                current = current.parentElement;
            }
            return null;
        }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.get(name) ?? null; }
        removeAttribute(name) { this.attributes.delete(name); }
        addEventListener(type, callback) {
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(callback);
        }
        dispatch(type, init = {}) {
            const event = {
                key: init.key,
                target: init.target || this,
                defaultPrevented: false,
                propagationStopped: false,
                preventDefault() { this.defaultPrevented = true; },
                stopPropagation() { this.propagationStopped = true; },
            };
            for (const callback of this.listeners.get(type) || []) callback(event);
            return event;
        }
        click() { return this.dispatch('click'); }
        focus() { this.focusCount += 1; }
    }

    globalThis.__buildSelect = ({ id, values, selectedIndex = 0, withInput = true }) => {
        const root = new FakeElement({ id, classes: ['pselect'] });
        root.dataset.value = values[selectedIndex]?.value || '';
        const trigger = new FakeElement({ classes: ['pselect-trigger'] });
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-disabled', values.length ? 'false' : 'true');
        const panel = new FakeElement({ classes: ['pselect-panel'] });
        panel.setAttribute('aria-hidden', 'true');
        const label = new FakeElement({
            classes: ['pselect-label'], text: values[selectedIndex]?.label || '请选择',
        });
        const input = withInput
            ? new FakeElement({ classes: ['pselect-input'], value: values[selectedIndex]?.value || '' })
            : null;
        const options = values.map((item, index) => {
            const option = new FakeElement({
                id: `${id}-option-${index}`,
                classes: ['pselect-option', ...(index === selectedIndex ? ['pselect-selected'] : [])],
                text: item.label,
            });
            option.dataset.value = item.value;
            option.setAttribute('aria-selected', index === selectedIndex ? 'true' : 'false');
            return option;
        });
        trigger.append(label);
        panel.append(...options);
        if (input) root.append(input);
        root.append(trigger, panel);
        return { root, trigger, panel, label, input, options };
    };

    const documentListeners = new Map();
    globalThis.document = {
        roots: [],
        addCounts: {},
        addEventListener(type, callback) {
            this.addCounts[type] = (this.addCounts[type] || 0) + 1;
            if (!documentListeners.has(type)) documentListeners.set(type, []);
            documentListeners.get(type).push(callback);
        },
        querySelectorAll(selector) {
            const found = [];
            for (const root of this.roots) {
                if (matches(root, selector)) found.push(root);
                found.push(...root.querySelectorAll(selector));
            }
            return found;
        },
        emit(type) {
            for (const callback of documentListeners.get(type) || []) callback({ target: this });
        },
    };
    globalThis.__FakeElement = FakeElement;
"""


def _custom_select_source_for_test() -> str:
    """只替换相邻转义依赖，保留真实选择框实现。"""

    source = CUSTOM_SELECT_CLIENT.read_text(encoding="utf-8")
    import_line = "import { escapeAttr, escapeHtml } from '../utils/ui.js';"
    assert import_line in source
    return source.replace(
        import_line,
        "const { escapeAttr, escapeHtml } = globalThis.__ui;",
    )


def _run_custom_select_client(script: str) -> None:
    """在最小 DOM 桩上执行选择框真实 ESM 模块。"""

    assert_node_esm_contract(
        _custom_select_source_for_test(),
        script,
        cwd=ROOT,
        setup=CUSTOM_SELECT_SETUP,
    )


def test_custom_select_rendering_is_escaped_unique_and_accessible() -> None:
    """渲染结果应拒绝无 ID、清洗类名，并只标记一个可访问选项。"""

    _run_custom_select_client(
        r"""
        const html = client.renderCustomSelect({
            id: 'demo',
            name: 'kind"><img',
            options: [
                { value: 'same', label: '第一项' },
                { value: 'same', label: '<b>重复项</b>' },
                3,
            ],
            selected: 'same',
            className: 'safe bad"><script pselect-block',
            labelledBy: 'demo-label',
        });

        assert.equal((html.match(/pselect-selected/g) || []).length, 1);
        assert.equal((html.match(/aria-selected="true"/g) || []).length, 1);
        assert.match(html, /role="combobox"/);
        assert.match(html, /aria-controls="demo-panel"/);
        assert.match(html, /role="listbox"/);
        assert.match(html, /role="option"/);
        assert.match(html, /aria-labelledby="demo-label"/);
        assert.match(html, /class="pselect safe pselect-block"/);
        assert.ok(html.includes('&lt;b&gt;重复项&lt;/b&gt;'));
        assert.ok(html.includes('name="kind&quot;&gt;&lt;img"'));
        assert.ok(!html.includes('<script'));

        const empty = client.renderCustomSelect({ id: 'empty', options: null });
        assert.match(empty, /pselect-disabled/);
        assert.match(empty, /tabindex="-1"/);
        assert.match(empty, /aria-disabled="true"/);
        assert.throws(() => client.renderCustomSelect({ options: [] }), TypeError);
        """
    )


def test_custom_select_keyboard_navigation_updates_value_and_aria_state() -> None:
    """方向键、首尾键、确认和 Escape 应维持同一选中与展开状态。"""

    _run_custom_select_client(
        r"""
        const first = __buildSelect({
            id: 'first',
            values: [
                { value: 'a', label: '甲' },
                { value: 'b', label: '乙' },
                { value: 'c', label: '丙' },
            ],
        });
        const second = __buildSelect({
            id: 'second', values: [{ value: 'x', label: '另一项' }],
        });
        const container = new __FakeElement();
        container.append(first.root, second.root);
        document.roots = [first.root, second.root];
        const changes = [];
        client.initCustomSelects(container, {
            first: (value) => changes.push(value),
        });

        assert.equal(document.addCounts.click, 1);
        let event = first.trigger.dispatch('keydown', { key: 'ArrowDown' });
        assert.equal(event.defaultPrevented, true);
        assert.equal(first.root.classList.contains('pselect-open'), true);
        assert.equal(first.trigger.getAttribute('aria-expanded'), 'true');
        assert.equal(first.panel.getAttribute('aria-hidden'), 'false');
        assert.equal(first.trigger.getAttribute('aria-activedescendant'), 'first-option-0');

        first.trigger.dispatch('keydown', { key: 'ArrowDown' });
        assert.equal(first.trigger.getAttribute('aria-activedescendant'), 'first-option-1');
        first.trigger.dispatch('keydown', { key: 'Enter' });
        assert.equal(first.root.dataset.value, 'b');
        assert.equal(first.label.textContent, '乙');
        assert.equal(first.input.value, 'b');
        assert.equal(first.options[1].getAttribute('aria-selected'), 'true');
        assert.equal(first.options[0].getAttribute('aria-selected'), 'false');
        assert.equal(first.root.classList.contains('pselect-open'), false);
        assert.deepEqual(changes, ['b']);
        assert.equal(first.trigger.focusCount, 1);

        first.trigger.dispatch('keydown', { key: 'End' });
        assert.equal(first.trigger.getAttribute('aria-activedescendant'), 'first-option-2');
        first.trigger.dispatch('keydown', { key: 'Escape' });
        assert.equal(first.root.classList.contains('pselect-open'), false);
        assert.equal(first.trigger.getAttribute('aria-activedescendant'), null);

        first.trigger.click();
        second.trigger.click();
        assert.equal(first.root.classList.contains('pselect-open'), false);
        assert.equal(second.root.classList.contains('pselect-open'), true);
        document.emit('click');
        assert.equal(second.root.classList.contains('pselect-open'), false);
        """
    )


def test_custom_select_replaces_callbacks_and_reports_async_failures() -> None:
    """重复初始化只更新 WeakMap 回调，旧回调不会残留，异步失败会被统一记录。"""

    _run_custom_select_client(
        r"""
        const select = __buildSelect({
            id: 'demo',
            values: [{ value: 'a', label: '甲' }, { value: 'b', label: '乙' }],
        });
        const container = new __FakeElement();
        container.append(select.root);
        document.roots = [select.root];
        const calls = [];

        client.initCustomSelects(container, { demo: () => calls.push('old') });
        client.initCustomSelects(container, {
            demo: async (value) => {
                calls.push(`new:${value}`);
                throw new Error('refresh failed');
            },
        });
        assert.equal(document.addCounts.click, 1);
        assert.equal(select.trigger.listeners.get('click').length, 1);

        select.trigger.click();
        select.panel.dispatch('click', { target: select.options[1] });
        await Promise.resolve();
        await Promise.resolve();
        assert.deepEqual(calls, ['new:b']);
        assert.equal(__errors.length, 1);
        assert.match(__errors[0], /自定义选择回调失败/);

        client.initCustomSelects(container, {});
        select.trigger.click();
        select.panel.dispatch('click', { target: select.options[0] });
        assert.deepEqual(calls, ['new:b']);
        """
    )
