"""Pendo Web 分页的数值、结构和异步交互回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
PAGINATION_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "pagination.js"
)

PAGINATION_SETUP: Final = r"""
    globalThis.__errors = [];
    globalThis.console.error = (...args) => {
        __errors.push(args.map((value) => String(value)).join(' '));
    };

    class FakeElement {
        constructor(tagName = 'div') {
            this.tagName = String(tagName).toLowerCase();
            this.className = '';
            this.type = '';
            this.textContent = '';
            this.dataset = {};
            this.disabled = false;
            this.children = [];
            this.attributes = new Map();
            this.onclick = null;
        }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.get(name) ?? null; }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        click() { return this.onclick?.(); }
    }

    globalThis.document = {
        createElement: (tagName) => new FakeElement(tagName),
    };
    globalThis.__FakeElement = FakeElement;
"""


def _run_pagination_client(script: str) -> None:
    """在最小 DOM 桩中执行分页真实 ESM 模块。"""

    assert_node_esm_contract(
        PAGINATION_CLIENT.read_text(encoding="utf-8"),
        script,
        cwd=ROOT,
        setup=PAGINATION_SETUP,
    )


def test_pagination_clears_single_pages_and_rejects_invalid_contracts() -> None:
    """无须分页时应清空旧节点，容器、参数和回调错误应立即暴露。"""

    _run_pagination_client(
        r"""
        const container = new __FakeElement();
        container.append(new __FakeElement('stale'));
        client.renderPagination(container, { page: 1, pageSize: 10, total: 10, onChange() {} });
        assert.equal(container.children.length, 0);

        container.append(new __FakeElement('stale'));
        client.renderPagination(container, {
            page: Symbol('bad'), pageSize: 0, total: Infinity, onChange() {},
        });
        assert.equal(container.children.length, 0);
        assert.throws(() => client.renderPagination(null, {}), TypeError);
        assert.throws(() => client.renderPagination(container, []), TypeError);
        assert.throws(
            () => client.renderPagination(container, { page: 1, pageSize: 10, total: 20 }),
            TypeError,
        );
        """
    )


def test_pagination_clamps_pages_and_builds_native_navigation_controls() -> None:
    """越界和小数输入应收敛为整数页，并生成带语义的原生按钮。"""

    _run_pagination_client(
        r"""
        const container = new __FakeElement();
        client.renderPagination(container, {
            page: 99, pageSize: '10.8', total: 23.9, onChange() {},
        });
        const navigation = container.children[0];
        const [previous, info, next] = navigation.children;
        assert.equal(navigation.tagName, 'nav');
        assert.equal(navigation.className, 'pagination');
        assert.equal(navigation.getAttribute('aria-label'), '分页');
        assert.equal(previous.type, 'button');
        assert.equal(next.type, 'button');
        assert.equal(previous.dataset.page, '2');
        assert.equal(previous.disabled, false);
        assert.equal(info.textContent, '3 / 3');
        assert.equal(info.getAttribute('aria-live'), 'polite');
        assert.equal(next.dataset.page, '4');
        assert.equal(next.disabled, true);

        client.renderPagination(container, {
            page: -8, pageSize: 10, total: 21, onChange() {},
        });
        const [firstPrevious, firstInfo, firstNext] = container.children[0].children;
        assert.equal(firstPrevious.disabled, true);
        assert.equal(firstInfo.textContent, '1 / 3');
        assert.equal(firstNext.dataset.page, '2');
        """
    )


def test_pagination_serializes_async_changes_and_recovers_after_failure() -> None:
    """切页期间应屏蔽重复点击，并在同步或异步失败后恢复按钮。"""

    _run_pagination_client(
        r"""
        const calls = [];
        let resolveChange;
        let changeImpl = (page) => new Promise((resolve) => {
            calls.push(page);
            resolveChange = resolve;
        });
        const container = new __FakeElement();
        client.renderPagination(container, {
            page: 2,
            pageSize: 10,
            total: 50,
            onChange: (page) => changeImpl(page),
        });
        const [previous, , next] = container.children[0].children;
        const pending = next.click();
        next.click();
        previous.click();
        assert.deepEqual(calls, [3]);
        assert.equal(previous.disabled, true);
        assert.equal(next.disabled, true);
        resolveChange();
        await pending;
        assert.equal(previous.disabled, false);
        assert.equal(next.disabled, false);

        changeImpl = () => { throw new Error('load failed'); };
        await previous.click();
        assert.equal(__errors.length, 1);
        assert.match(__errors[0], /分页切换失败.*load failed/);
        assert.equal(previous.disabled, false);
        assert.equal(next.disabled, false);
        """
    )
