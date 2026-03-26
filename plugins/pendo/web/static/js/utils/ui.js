export function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export function injectStyles(styleId, cssText) {
    if (document.getElementById(styleId)) return;
    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = cssText;
    document.head.appendChild(style);
}

export function pageShellCss(className, options = {}) {
    const {
        padding = '26px 24px 36px',
        maxWidth = '1280px',
        margin = '0 auto',
    } = options;
    return `.${className} { padding: ${padding}; max-width: ${maxWidth}; margin: ${margin}; }`;
}
