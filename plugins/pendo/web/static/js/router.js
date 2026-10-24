/** Pendo Web 的 hash 路由注册、页面生命周期和路由订阅边界。 */

import { escapeHtml } from './utils/ui.js';
import { fetchUserTimeZone } from './utils/timezone.js';

const routes               = new Map();
const routeChangeCallbacks = new Set();

let contentEl           = null;
let currentLifecycle    = null;
let hashListenerBound   = false;
let requestedNavigation = 0;

export function registerRoute(path, loader) {
    routes.set(path, loader);
}

export function navigate(path) {
    window.location.hash = `#/${path}`;
}

export function getParams() {
    const hash       = window.location.hash.replace(/^#\/?/, '');
    const queryIndex = hash.indexOf('?');
    const path       = queryIndex >= 0 ? hash.slice(0, queryIndex) : hash;
    const params     = new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : '');
    return { path: path || 'dashboard', params };
}

function destroyLifecycle(lifecycle) {
    if (!lifecycle || lifecycle.destroyed) return;
    lifecycle.destroyed = true;

    if (typeof lifecycle.page?.destroy !== 'function') return;
    try {
        lifecycle.page.destroy();
    } catch (cause) {
        console.error('Pendo Web 页面清理失败:', cause);
    }
}

function destroyCurrentPage() {
    const lifecycle = currentLifecycle;
    currentLifecycle = null;
    destroyLifecycle(lifecycle);
}

async function loadRoute({ path, params }, navigationId) {
    const loader = routes.get(path);
    if (!loader) {
        contentEl.innerHTML = '<div class="empty-state"><p>页面不存在</p></div>';
        return;
    }

    let lifecycle = null;
    try {
        if (path !== 'settings') await fetchUserTimeZone();
        if (navigationId !== requestedNavigation) return;
        const page = await loader();
        if (navigationId !== requestedNavigation) return;

        lifecycle = { page, destroyed: false };
        currentLifecycle = lifecycle;
        const pageRoot = document.createElement('div');
        pageRoot.className = 'route-page';
        contentEl.replaceChildren(pageRoot);
        await page.onRouteEnter?.(params);
        if (navigationId !== requestedNavigation) return;
        await page.render?.(pageRoot);

        if (navigationId !== requestedNavigation) {
            if (currentLifecycle === lifecycle) currentLifecycle = null;
            destroyLifecycle(lifecycle);
        }
    } catch (cause) {
        if (navigationId !== requestedNavigation) {
            destroyLifecycle(lifecycle);
            return;
        }

        console.error('Pendo Web 页面加载失败:', cause);
        const message = cause instanceof Error ? cause.message : String(cause ?? '未知错误');
        if (currentLifecycle === lifecycle) currentLifecycle = null;
        destroyLifecycle(lifecycle);
        contentEl.innerHTML = `<div class="error-state"><p>加载失败：${escapeHtml(message)}</p><button type="button" class="btn btn-primary" id="route-retry">重新加载</button></div>`;
        const retry = contentEl.querySelector?.('#route-retry');
        if (retry) retry.onclick = queueCurrentRoute;
    }
}

function queueCurrentRoute() {
    const navigationId = ++requestedNavigation;
    const route        = getParams();

    // 新导航立即独立加载。旧请求只能写入已移出的页面容器。
    destroyCurrentPage();
    contentEl.innerHTML = '<div class="empty-state"><p>加载中…</p></div>';

    return loadRoute(route, navigationId);
}

function notifyRouteChange() {
    const path = getParams().path;
    for (const callback of routeChangeCallbacks) {
        try {
            callback(path);
        } catch (cause) {
            console.error('Pendo Web 路由订阅回调失败:', cause);
        }
    }
}

function handleHashChange() {
    notifyRouteChange();
    if (contentEl) return queueCurrentRoute();
    return Promise.resolve();
}

function ensureHashListener() {
    if (hashListenerBound) return;
    window.addEventListener('hashchange', handleHashChange);
    hashListenerBound = true;
}

export async function init(container) {
    contentEl = container;
    ensureHashListener();
    await queueCurrentRoute();
}

export function onRouteChange(callback) {
    routeChangeCallbacks.add(callback);
    ensureHashListener();
    try {
        callback(getParams().path);
    } catch (cause) {
        console.error('Pendo Web 路由订阅回调失败:', cause);
    }
    return () => routeChangeCallbacks.delete(callback);
}
