/** Pendo Web 共享的 HTML 转义和页面级 DOM 生命周期工具。 */

export function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/** 当前属性值统一使用双引号，所需转义规则与文本节点完全相同。 */
export { escapeHtml as escapeAttr };

/** 订阅跨页面数据失效事件，并返回可重复调用的退订函数。 */
export function subscribeDataChanges(expectedType, refresh) {
    let active    = true;
    const handler = (event) => {
        const changedType = event?.detail?.type;
        // 省略 type 表示全局失效。带 type 时只刷新对应页面。
        if (expectedType && changedType && changedType !== expectedType) return;
        return refresh(event);
    };
    window.addEventListener('pendo-data-changed', handler);

    return () => {
        if (!active) return;
        active = false;
        window.removeEventListener('pendo-data-changed', handler);
    };
}

/** 把表单提交统一转交给已承载防重与加载状态的主按钮。 */
export function bindFormSubmit(form, submitButton) {
    if (!form || !submitButton) return;
    form.onsubmit = (event) => {
        event.preventDefault();
        submitButton.click();
    };
}

/** 给日期等单行输入绑定一致的非输入法回车动作。 */
export function bindEnterAction(element, action) {
    if (!element || typeof action !== 'function') return;
    element.onkeydown = async (event) => {
        if (event.key === 'Enter' && !event.isComposing) {
            event.preventDefault();
            await action();
        }
    };
}
