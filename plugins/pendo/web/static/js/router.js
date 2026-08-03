/** Pendo Web 的 hash 路由注册、页面生命周期和路由订阅边界。 */

import { escapeHtml } from './utils/ui.js';

const routes = new Map();
const routeChangeCallbacks = new Set();

let contentEl = null;
let currentLifecycle = null;
let hashListenerBound = false;
let requestedNavigation = 0;
let routeLoadChain = Promise.resolve();

export function registerRoute(path, loader) {
    routes.set(path, loader);
}

export function navigate(path) {
    window.location.hash = `#/${path}`;
}

export function getParams() {
    const hash = window.location.hash.replace(/^#\/?/, '');
    const queryIndex = hash.indexOf('?');
    const path = queryIndex >= 0 ? hash.slice(0, queryIndex) : hash;
    const params = new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : '');
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
        const page = await loader();
        if (navigationId !== requestedNavigation) return;

        lifecycle = { page, destroyed: false };
        currentLifecycle = lifecycle;
        contentEl.innerHTML = '';
        await page.onRouteEnter?.(params);
        await page.render?.(contentEl);

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
        contentEl.innerHTML = `<div class="error-state"><p>加载失败：${escapeHtml(message)}</p></div>`;
    }
}

function queueCurrentRoute() {
    const navigationId = ++requestedNavigation;
    const route = getParams();

    // 新导航立即让旧页停止监听；异步收尾完成后只装载最后一次请求。
    destroyCurrentPage();
    contentEl.innerHTML = '<div class="empty-state"><p>加载中…</p></div>';

    const loadLatestRoute = async () => {
        if (navigationId !== requestedNavigation) return;
        await loadRoute(route, navigationId);
    };
    // 即使此前队列因意外异常拒绝，下一次导航也应能继续工作。
    routeLoadChain = routeLoadChain.then(loadLatestRoute, loadLatestRoute);
    return routeLoadChain;
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
