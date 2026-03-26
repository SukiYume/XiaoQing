const routes = {};
let currentPage = null;
let contentEl = null;

export function registerRoute(path, loader) {
    routes[path] = loader;
}

export function navigate(path) {
    window.location.hash = '#/' + path;
}

export function getParams() {
    const hash = window.location.hash.slice(2) || '';
    const qIdx = hash.indexOf('?');
    const path = qIdx >= 0 ? hash.slice(0, qIdx) : hash;
    const params = new URLSearchParams(qIdx >= 0 ? hash.slice(qIdx + 1) : '');
    return { path: path || 'dashboard', params };
}

export function getCurrentPage() {
    return getParams().path;
}

export async function init(container) {
    contentEl = container;
    window.addEventListener('hashchange', () => loadCurrentRoute());
    await loadCurrentRoute();
}

async function loadCurrentRoute() {
    const { path, params } = getParams();

    if (currentPage && currentPage.destroy) {
        currentPage.destroy();
    }

    const loader = routes[path];
    if (!loader) {
        contentEl.innerHTML = '<div class="empty-state"><p>页面不存在</p></div>';
        currentPage = null;
        return;
    }

    try {
        contentEl.innerHTML = '<div class="empty-state"><p>加载中...</p></div>';
        const page = await loader();
        currentPage = page;
        contentEl.innerHTML = '';
        if (page.onRouteEnter) page.onRouteEnter(params);
        if (page.render) page.render(contentEl);
    } catch (e) {
        console.error('Page load error:', e);
        contentEl.innerHTML = `<div class="error-state"><p>加载失败: ${e.message}</p></div>`;
        currentPage = null;
    }
}

// Re-export for sidebar to highlight active
export function onRouteChange(callback) {
    window.addEventListener('hashchange', () => callback(getParams().path));
    // Call immediately for initial state
    callback(getParams().path);
}
