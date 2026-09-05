"""Pendo Web 通用表单的安全渲染、取值和交互回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final        = REPOSITORY_ROOT
FORM_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "form.js"

FORM_SETUP: Final = r"""
    globalThis.__ui = {
        escapeHtml: (value) => String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;'),
    };
    __ui.escapeAttr = __ui.escapeHtml;
    globalThis.__renderCalls = [];
    globalThis.__customInitCalls = [];
    globalThis.__customSelect = {
        renderCustomSelect: (options) => {
            __renderCalls.push(options);
            return `<div class="pselect" id="${__ui.escapeAttr(options.id)}">`
                + `<button id="${__ui.escapeAttr(options.id)}-trigger"></button></div>`;
        },
        initCustomSelects: (container) => __customInitCalls.push(container),
    };
"""


def _form_source_for_test() -> str:
    """只替换相邻模块依赖，保留表单模块的真实实现。"""

    source = FORM_CLIENT.read_text(encoding="utf-8")
    custom_import = "import { initCustomSelects, renderCustomSelect } from './custom_select.js';"
    ui_import     = "import { escapeAttr, escapeHtml } from '../utils/ui.js';"
    assert custom_import in source
    assert ui_import in source
    return source.replace(
        custom_import,
        "const { initCustomSelects, renderCustomSelect } = globalThis.__customSelect;",
    ).replace(
        ui_import,
        "const { escapeAttr, escapeHtml } = globalThis.__ui;",
    )


def _run_form_client(script: str) -> None:
    """在最小浏览器桩中执行表单真实 ESM 模块。"""

    assert_node_esm_contract(
        _form_source_for_test(),
        script,
        cwd   = ROOT,
        setup = FORM_SETUP,
    )


def test_form_rendering_escapes_values_and_builds_accessible_single_choice_groups() -> None:
    """所有字段应只转义一次，并为两类按钮组保留唯一选中状态。"""

    assert "case 'tags':" not in FORM_CLIENT.read_text(encoding="utf-8")
    _run_form_client(
        r"""
        const html = client.buildFormHTML([
            {
                type: 'select', id: 'kind"><id', name: 'kind"><name',
                label: '<b>类型</b>', value: 'a', placeholder: '请选择',
                options: [{ value: 'a', label: '甲' }],
            },
            {
                type: 'textarea', name: 'body', label: '正文', rows: 6,
                value: '</textarea><script>bad()</script>', required: true,
            },
            { type: 'datetime', name: 'at', label: '时间', value: '2024-01-02 03:04' },
            { type: 'date', name: 'day', label: '日期', value: '2024-01-02' },
            { type: 'number', name: 'open', label: '任意数字', value: 0 },
            {
                type: 'number', name: 'bounded', label: '范围', value: 2,
                min: 0, max: 5, step: 0.5,
            },
            {
                type: 'checkbox', name: 'enabled', label: '状态', value: true,
                checkboxLabel: '<img src=x>', required: true,
            },
            { type: 'priority', name: 'priority', label: '优先级', value: 2 },
            {
                type: 'mood', name: 'mood', label: '心情', value: 'same',
                options: [
                    { value: 'same', label: '首项', emoji: '<首>' },
                    { value: 'same', label: '"><script>', emoji: '<次>' },
                ],
            },
            { name: 'title', label: '标题', value: '"><script>bad()</script>' },
        ]);

        assert.equal(__renderCalls.length, 1);
        assert.equal(__renderCalls[0].id, 'kind"><id');
        assert.equal(__renderCalls[0].name, 'kind"><name');
        assert.equal(__renderCalls[0].labelledBy, 'kind"><id-label');
        assert.ok(html.includes('for="kind&quot;&gt;&lt;id-trigger"'));
        assert.ok(html.includes('id="kind&quot;&gt;&lt;id-label"'));
        assert.ok(!html.includes('&amp;quot;'));
        assert.ok(html.includes('&lt;b&gt;类型&lt;/b&gt;'));
        assert.ok(html.includes('&lt;/textarea&gt;&lt;script&gt;bad()&lt;/script&gt;'));
        assert.ok(!html.includes('</textarea><script>'));
        assert.ok(html.includes('&lt;img src=x&gt;'));
        assert.ok(html.includes('name="enabled" class="form-input" aria-labelledby='));
        assert.ok(html.includes(' checked required'));

        const openNumber = html.match(/<input[^>]+name="open"[^>]+>/)[0];
        assert.ok(!openNumber.includes(' min='));
        assert.ok(!openNumber.includes(' max='));
        assert.ok(openNumber.includes(' step="any"'));
        const bounded = html.match(/<input[^>]+name="bounded"[^>]+>/)[0];
        assert.ok(bounded.includes(' min="0"'));
        assert.ok(bounded.includes(' max="5"'));
        assert.ok(bounded.includes(' step="0.5"'));

        assert.equal((html.match(/priority-btn[^"\n]* active/g) || []).length, 1);
        assert.ok(html.includes('class="priority-btn priority-2 active"'));
        assert.match(html, /priority-selector[^>]+role="group"[^>]+aria-labelledby=/);
        assert.equal((html.match(/mood-btn active/g) || []).length, 1);
        assert.ok(html.includes('data-value="same" title="首项"'));
        assert.ok(html.includes('&lt;首&gt;'));
        assert.ok(html.includes('&quot;&gt;&lt;script&gt;'));
        assert.ok(!html.includes('<script>'));
        assert.match(html, /mood-selector[^>]+role="group"[^>]+aria-labelledby=/);
        assert.throws(() => client.buildFormHTML(null), TypeError);
        """
    )


def test_form_data_normalizes_empty_invalid_and_mutually_exclusive_values() -> None:
    """取值应保留布尔值与有限数字，并安全忽略损坏的按钮组。"""

    _run_form_client(
        r"""
        const input = (name, type, value, checked = false) => ({
            name, type, value, checked,
        });
        const priorityGroup = { dataset: { name: 'priority' } };
        const moodGroup = { dataset: { name: 'mood' } };
        const priority = {
            dataset: { value: '4' },
            closest: () => priorityGroup,
        };
        const brokenPriority = {
            dataset: { value: '4.5' },
            closest: () => ({ dataset: { name: 'brokenPriority' } }),
        };
        const mood = {
            dataset: { value: 'calm' },
            closest: () => moodGroup,
        };
        const orphanMood = { dataset: {}, closest: () => null };
        const inputs = [
            input('enabled', 'checkbox', 'on', true),
            input('disabled', 'checkbox', 'on', false),
            input('count', 'number', ' 12.5 '),
            input('zero', 'number', '0'),
            input('emptyNumber', 'number', '  '),
            input('badNumber', 'number', '12px'),
            input('infiniteNumber', 'number', 'Infinity'),
            input('emptyText', 'text', ''),
            input('spaceText', 'text', '   '),
            input('kind', 'hidden', 'task'),
            input('', 'text', 'ignored'),
        ];
        const container = {
            querySelectorAll(selector) {
                if (selector === '.form-input') return inputs;
                if (selector === '.priority-selector .active') {
                    return [priority, brokenPriority];
                }
                if (selector === '.mood-selector .active') return [mood, orphanMood];
                return [];
            },
        };

        assert.deepEqual(client.getFormData(container), {
            enabled: true,
            disabled: false,
            count: 12.5,
            zero: 0,
            emptyNumber: null,
            badNumber: null,
            infiniteNumber: null,
            emptyText: null,
            spaceText: '   ',
            kind: 'task',
            priority: 4,
            mood: 'calm',
        });
        assert.deepEqual(client.getFormData(null), {});
        """
    )


def test_form_interactions_share_one_idempotent_exclusive_button_path() -> None:
    """重复初始化后一次点击仍只更新一次，并同步 active 与 aria-pressed。"""

    _run_form_client(
        r"""
        class FakeClassList {
            constructor(active = false) {
                this.values = new Set(active ? ['active'] : []);
                this.toggleCalls = 0;
            }
            contains(name) { return this.values.has(name); }
            toggle(name, force) {
                this.toggleCalls += 1;
                if (force) this.values.add(name);
                else this.values.delete(name);
            }
        }
        const button = (active = false) => ({
            classList: new FakeClassList(active),
            attributes: new Map([['aria-pressed', active ? 'true' : 'false']]),
            onclick: null,
            setAttribute(name, value) { this.attributes.set(name, String(value)); },
        });
        const priorityButtons = [button(true), button(false), button(false)];
        const moodButtons = [button(false), button(true)];
        const group = (buttons) => ({ querySelectorAll: () => buttons });
        const priorityGroup = group(priorityButtons);
        const moodGroup = group(moodButtons);
        const container = {
            querySelectorAll(selector) {
                if (selector === '.priority-selector') return [priorityGroup];
                if (selector === '.mood-selector') return [moodGroup];
                return [];
            },
        };

        client.initFormInteractions(container);
        client.initFormInteractions(container);
        assert.equal(__customInitCalls.length, 2);
        priorityButtons[2].onclick();
        moodButtons[0].onclick();

        assert.deepEqual(
            priorityButtons.map((item) => item.classList.contains('active')),
            [false, false, true],
        );
        assert.deepEqual(
            priorityButtons.map((item) => item.attributes.get('aria-pressed')),
            ['false', 'false', 'true'],
        );
        assert.deepEqual(
            moodButtons.map((item) => item.classList.contains('active')),
            [true, false],
        );
        assert.deepEqual(
            moodButtons.map((item) => item.attributes.get('aria-pressed')),
            ['true', 'false'],
        );
        assert.deepEqual(
            priorityButtons.map((item) => item.classList.toggleCalls),
            [1, 1, 1],
        );

        client.initFormInteractions(null);
        assert.equal(__customInitCalls.length, 2);
        """
    )
