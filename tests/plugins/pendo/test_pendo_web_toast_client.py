"""Pendo Web 全局提示的安全渲染、类型和生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final         = REPOSITORY_ROOT
TOAST_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "toast.js"
)

TOAST_SETUP: Final = r"""
    class FakeClassList {
        constructor() { this.values = new Set(); }
        add(...names) { names.forEach((name) => this.values.add(name)); }
        remove(...names) { names.forEach((name) => this.values.delete(name)); }
        contains(name) { return this.values.has(name); }
    }

    class FakeElement {
        constructor(tagName = 'div') {
            this.tagName = String(tagName).toLowerCase();
            this.className = '';
            this.classList = new FakeClassList();
            this.type = '';
            this.textContent = '';
            this.children = [];
            this.parentElement = null;
            this.attributes = new Map();
            this.listeners = new Map();
            this.removed = false;
        }
        append(...children) {
            for (const child of children) {
                child.parentElement = this;
                this.children.push(child);
            }
        }
        appendChild(child) { this.append(child); return child; }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.get(name) ?? null; }
        addEventListener(type, callback) {
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(callback);
        }
        click() {
            for (const callback of this.listeners.get('click') || []) callback({ target: this });
        }
        remove() {
            this.removed = true;
            if (!this.parentElement) return;
            this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
            this.parentElement = null;
        }
    }

    const toastContainer = new FakeElement('div');
    globalThis.document = {
        getElementById: (id) => id === 'toast-container' ? toastContainer : null,
        createElement: (tagName) => new FakeElement(tagName),
    };

    const animationFrames = [];
    globalThis.requestAnimationFrame = (callback) => {
        animationFrames.push(callback);
        return animationFrames.length;
    };
    globalThis.__runAnimationFrames = () => {
        while (animationFrames.length) animationFrames.shift()();
    };

    let nextTimerId = 1;
    const timers = new Map();
    globalThis.setTimeout = (callback, delay) => {
        const id = nextTimerId++;
        timers.set(id, { callback, delay });
        return id;
    };
    globalThis.clearTimeout = (id) => timers.delete(id);
    globalThis.__pendingTimerDelays = () => [...timers.values()].map(({ delay }) => delay);
    globalThis.__runTimer = (delay) => {
        const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
        assert.ok(entry, `找不到延迟 ${delay}ms 的计时器`);
        const [id, timer] = entry;
        timers.delete(id);
        timer.callback();
    };
    globalThis.__toastContainer = toastContainer;
"""


def _run_toast_client(script: str) -> None:
    """在最小 DOM 和可控计时器中执行 Toast 真实 ESM 模块。"""

    assert_node_esm_contract(
        TOAST_CLIENT.read_text(encoding="utf-8"),
        script,
        cwd   = ROOT,
        setup = TOAST_SETUP,
    )


def test_toast_renders_message_as_text_and_builds_accessible_native_controls() -> None:
    """动态消息不得进入 HTML 解析，关闭控件必须保留按钮语义。"""

    _run_toast_client(
        r"""
        client.showToast('<img src=x onerror=alert(1)>', ' ERROR ');
        const toast = __toastContainer.children[0];
        const [message, dismiss] = toast.children;

        assert.equal(toast.className, 'toast toast-error');
        assert.equal(message.tagName, 'span');
        assert.equal(message.className, 'toast-message');
        assert.equal(message.textContent, '<img src=x onerror=alert(1)>');
        assert.equal(message.children.length, 0);
        assert.equal(dismiss.tagName, 'button');
        assert.equal(dismiss.type, 'button');
        assert.equal(dismiss.className, 'toast-dismiss');
        assert.equal(dismiss.getAttribute('aria-label'), '关闭提示');

        __runAnimationFrames();
        assert.equal(toast.classList.contains('toast-show'), true);
        """
    )


def test_toast_accepts_only_existing_types_and_requires_its_mount_point() -> None:
    """类型白名单应阻止任意类名，缺少静态容器时应立即给出清晰错误。"""

    _run_toast_client(
        r"""
        for (const type of ['success', 'error', 'info', 'warning']) {
            client.showToast(type, type);
            assert.equal(__toastContainer.children.at(-1).className, `toast toast-${type}`);
        }
        client.showToast('unknown', 'error extra-class');
        assert.equal(__toastContainer.children.at(-1).className, 'toast toast-info');
        client.showToast('non-string', { toString: () => 'success' });
        assert.equal(__toastContainer.children.at(-1).className, 'toast toast-info');

        document.getElementById = () => null;
        assert.throws(() => client.showToast('missing'), /缺少 #toast-container 提示容器/);
        """
    )


def test_toast_manual_and_automatic_dismissal_share_one_idempotent_lifecycle() -> None:
    """手动与自动关闭都只能安排一次退场，并在动画结束后移除节点。"""

    _run_toast_client(
        r"""
        client.showToast('manual');
        const manualToast = __toastContainer.children[0];
        const dismiss = manualToast.children[1];
        assert.deepEqual(__pendingTimerDelays(), [3000]);

        dismiss.click();
        dismiss.click();
        __runAnimationFrames();
        assert.equal(manualToast.classList.contains('toast-show'), false);
        assert.deepEqual(__pendingTimerDelays(), [250]);
        __runTimer(250);
        assert.equal(manualToast.removed, true);
        assert.equal(__toastContainer.children.length, 0);

        client.showToast('automatic', 'success');
        const automaticToast = __toastContainer.children[0];
        __runAnimationFrames();
        assert.equal(automaticToast.classList.contains('toast-show'), true);
        __runTimer(3000);
        assert.equal(automaticToast.classList.contains('toast-show'), false);
        assert.deepEqual(__pendingTimerDelays(), [250]);
        __runTimer(250);
        assert.equal(automaticToast.removed, true);
        assert.equal(__toastContainer.children.length, 0);
        """
    )
