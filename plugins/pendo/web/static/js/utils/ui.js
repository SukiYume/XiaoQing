/** Pendo Web 共享的 HTML 转义、响应式 CSS 和页面级 DOM 生命周期工具。 */

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

export const BREAKPOINTS = Object.freeze({
    XL: '1200px',
    MOBILE: '720px',
    PHONE: '560px',
});

export function mediaMax(breakpoint, cssText) {
    return `@media (max-width: ${breakpoint}) { ${cssText} }`;
}

export function pageShellCss(className, options = {}) {
    const {
        padding = '26px 24px 36px',
        maxWidth = '1280px',
        margin = '0 auto',
        compactPadding = null,
        compactBreakpoint = BREAKPOINTS.MOBILE,
    } = options;
    return `
        .${className} {
            width: 100%;
            min-width: 0;
            max-width: ${maxWidth};
            margin: ${margin};
            padding: ${padding};
            box-sizing: border-box;
        }
        ${compactPadding ? `@media (max-width: ${compactBreakpoint}) { .${className} { padding: ${compactPadding}; } }` : ''}
    `;
}

/** 按稳定 ID 创建或更新页面样式，重复渲染不会累积 style 节点。 */
export function injectStyles(styleId, cssText) {
    const existing = document.getElementById(styleId);
    if (existing) {
        if (existing.textContent !== cssText) existing.textContent = cssText;
        return;
    }

    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = cssText;
    document.head.appendChild(style);
}

/** 订阅跨页面数据失效事件，并返回可重复调用的退订函数。 */
export function subscribeDataChanges(expectedType, refresh) {
    let active    = true;
    const handler = (event) => {
        const changedType = event?.detail?.type;
        // 省略 type 表示全局失效；带 type 时只刷新对应页面。
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
