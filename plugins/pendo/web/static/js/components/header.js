/** Pendo Web 顶栏的页面标题、全局搜索和会话操作。 */

import { logout } from '../api.js';
import { navigate, onRouteChange } from '../router.js';

const PAGE_TITLES = Object.freeze({
    dashboard: '总览',
    events: '日程',
    tasks: '待办',
    ledger: '记账',
    notes: '笔记',
    diary: '日记',
    search: '搜索',
    stats: '统计',
    settings: '设置',
    transfer: '数据迁移',
});

export function renderHeader(container) {
    if (!container?.appendChild) throw new TypeError('顶栏容器无效');

    const header = document.createElement('header');
    header.className = 'header';
    header.innerHTML = `
        <button class="header-toggle" type="button" aria-label="打开导航菜单" aria-controls="pendo-sidebar" aria-expanded="false" title="菜单">☰</button>
        <h2 class="header-title">总览</h2>
        <div class="header-actions">
            <div class="header-search">
                <span class="header-search-icon" aria-hidden="true">
                    <svg viewBox="0 0 20 20" fill="none" focusable="false">
                        <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" stroke-width="1.8"></circle>
                        <path d="M12.5 12.5L17 17" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"></path>
                    </svg>
                </span>
                <input id="header-search-input" name="global-search" type="text" placeholder="搜索..." class="header-search-input" aria-label="全局搜索">
            </div>
            <button class="header-search-toggle" type="button" aria-label="打开搜索" title="搜索">⌕</button>
            <button class="btn btn-ghost header-logout" type="button" title="退出登录">退出</button>
        </div>
    `;
    container.appendChild(header);

    const titleElement = header.querySelector('.header-title');
    onRouteChange((path) => {
        // 未知路径保留原值，便于识别路由配置缺口；textContent 不会执行路径内容。
        titleElement.textContent = PAGE_TITLES[path] || path;
    });

    header.querySelector('.header-toggle').addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent('pendo:toggle-sidebar'));
    });

    const searchInput = header.querySelector('.header-search-input');
    searchInput.addEventListener('keydown', (event) => {
        // 中文输入法确认候选词时也会发出 Enter，组合结束前不能提交搜索。
        if (event.key !== 'Enter' || event.isComposing) return;

        const query = searchInput.value.trim();
        if (!query) return;
        navigate(`search?q=${encodeURIComponent(query)}`);
        searchInput.value = '';
    });
    header.querySelector('.header-search-toggle').addEventListener('click', () => {
        navigate('search');
    });

    const logoutButton = header.querySelector('.header-logout');
    logoutButton.addEventListener('click', async () => {
        if (logoutButton.disabled) return;
        logoutButton.disabled = true;
        try {
            await logout();
            window.location.reload();
        } catch (cause) {
            // 网络失败时保留当前会话并允许重试，避免事件回调产生未处理拒绝。
            console.error('Pendo Web 退出登录失败:', cause);
            logoutButton.disabled = false;
        }
    });
}
