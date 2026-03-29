import { onRouteChange, navigate } from '../router.js';
import { clearToken } from '../api.js';

const PAGE_TITLES = {
    dashboard: '总览', events: '日程', tasks: '待办', ledger: '记账',
    notes: '笔记', diary: '日记', search: '搜索', stats: '统计', settings: '设置',
    transfer: '数据迁移',
};

export function renderHeader(container) {
    const header = document.createElement('header');
    header.className = 'header';
    header.innerHTML = `
        <button class="header-toggle" type="button" aria-label="打开导航菜单" title="菜单">☰</button>
        <h2 class="header-title">总览</h2>
        <div class="header-actions">
            <div class="header-search">
                <span class="header-search-icon" aria-hidden="true">
                    <svg viewBox="0 0 20 20" fill="none" focusable="false">
                        <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" stroke-width="1.8"></circle>
                        <path d="M12.5 12.5L17 17" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"></path>
                    </svg>
                </span>
                <input id="header-search-input" name="global-search" type="text" placeholder="搜索..." class="header-search-input" />
            </div>
            <button class="header-search-toggle" type="button" aria-label="打开搜索" title="搜索">⌕</button>
            <button class="btn btn-ghost header-logout" title="退出登录">退出</button>
        </div>
    `;
    container.appendChild(header);

    // Update title on route change
    const titleEl = header.querySelector('.header-title');
    onRouteChange(path => {
        titleEl.textContent = PAGE_TITLES[path] || path;
    });

    header.querySelector('.header-toggle').addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent('pendo:toggle-sidebar'));
    });

    // Search
    const searchInput = header.querySelector('.header-search-input');
    searchInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && searchInput.value.trim()) {
            navigate('search?q=' + encodeURIComponent(searchInput.value.trim()));
            searchInput.value = '';
        }
    });
    header.querySelector('.header-search-toggle').addEventListener('click', () => {
        navigate('search');
    });

    // Logout
    header.querySelector('.header-logout').addEventListener('click', () => {
        clearToken();
        window.location.reload();
    });
}
