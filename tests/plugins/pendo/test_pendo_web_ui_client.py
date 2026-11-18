"""Pendo Web UI 共享工具的转义和数据订阅。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT     : Final = REPOSITORY_ROOT
UI_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "ui.js"

UI_SETUP: Final = r"""
    globalThis.__state = { removeCount: 0 };

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
    """在事件桩上导入真实 UI 工具模块。"""

    assert_node_esm_contract(
        UI_CLIENT.read_text(encoding="utf-8"),
        script,
        cwd   = ROOT,
        setup = UI_SETUP,
    )


def test_ui_client_escapes_markup_for_text_and_attributes() -> None:
    """文本与属性共用安全转义。"""

    _run_ui_client(
        r"""
        assert.equal(client.escapeHtml(null), '');
        assert.equal(client.escapeHtml('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
        assert.equal(client.escapeHtml(0), '0');
        assert.equal(client.escapeAttr, client.escapeHtml);
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
