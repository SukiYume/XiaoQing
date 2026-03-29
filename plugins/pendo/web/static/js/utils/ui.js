export function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export const BREAKPOINTS = Object.freeze({
    XL: '1200px',
    WIDE: '1120px',
    DESKTOP: '1024px',
    DASHBOARD: '920px',
    FORM: '980px',
    COMPACT: '860px',
    SEARCH: '820px',
    EVENTS: '840px',
    EVENTS_WIDE: '1100px',
    NARROW: '760px',
    MOBILE: '720px',
    PHONE: '560px',
    XS: '480px',
    STATS_SMALL: '640px',
});

export function mediaMax(breakpoint, cssText) {
    return `@media (max-width: ${breakpoint}) { ${cssText} }`;
}

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

export function pageShellCss(className, options = {}) {
    const {
        padding = '26px 24px 36px',
        maxWidth = '1280px',
        margin = '0 auto',
        compactPadding = null,
        compactBreakpoint = '760px',
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
