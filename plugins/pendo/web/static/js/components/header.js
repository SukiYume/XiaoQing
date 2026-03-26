import { onRouteChange, navigate } from '../router.js';
import { clearToken } from '../api.js';

const PAGE_TITLES = {
    dashboard: '总览', events: '日程', tasks: '待办', ledger: '记账',
    notes: '笔记', diary: '日记', search: '搜索', stats: '统计', settings: '设置',
};

export function renderHeader(container) {
    const header = document.createElement('header');
    header.className = 'header';
    header.innerHTML = `
        <h2 class="header-title">总览</h2>
        <div class="header-actions">
            <div class="header-search">
                <input type="text" placeholder="搜索..." class="header-search-input" />
            </div>
            <button class="btn btn-ghost header-logout" title="退出登录">退出</button>
        </div>
    `;
    container.appendChild(header);

    // Update title on route change
    const titleEl = header.querySelector('.header-title');
    onRouteChange(path => {
        titleEl.textContent = PAGE_TITLES[path] || path;
    });

    // Search
    const searchInput = header.querySelector('.header-search-input');
    searchInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && searchInput.value.trim()) {
            navigate('search?q=' + encodeURIComponent(searchInput.value.trim()));
            searchInput.value = '';
        }
    });

    // Logout
    header.querySelector('.header-logout').addEventListener('click', () => {
        clearToken();
        window.location.reload();
    });
}
